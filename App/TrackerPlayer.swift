import SwiftUI
import AppKit
import AVKit
import UniformTypeIdentifiers
import Darwin
import ImageIO

typealias Object = [String: Any]
func string(_ o: Object, _ k: String, _ fallback: String = "") -> String { o[k] as? String ?? fallback }
func number(_ o: Object, _ k: String) -> Int { (o[k] as? NSNumber)?.intValue ?? 0 }
func objects(_ value: Any?) -> [Object] { value as? [Object] ?? [] }
func gigabytes(_ bytes:Int) -> String {String(format:"%.3f GB",Double(bytes)/1_000_000_000)}
func timestamp(_ value: String) -> String { String(value.prefix(16)).replacingOccurrences(of: "T", with: " · ") }
func durationText(_ t: Double) -> String {
    guard t.isFinite && t > 0 else { return "0:00" }
    return String(format: "%d:%02d", Int(t) / 60, Int(t) % 60)
}
extension Color {
    init(hex: String) {
        let value = UInt64(hex.trimmingCharacters(in: CharacterSet(charactersIn: "#")), radix: 16) ?? 0xDFAE70
        self.init(red: Double((value >> 16) & 255) / 255, green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255)
    }
}

struct Track: Identifiable, Hashable {
    let id: String, title: String, name: String, era: String, workbook: String, version: String, kind: String, availability: String
    let availableLength: String, quality: String, trackLength: String
    let sourceRow: Int, sourceCount: Int
    let ambiguous: Bool, eligible: Bool
    init(_ o: Object) {
        id=string(o,"id"); title=string(o,"title"); name=string(o,"name",string(o,"title")); era=string(o,"era"); workbook=string(o,"workbook"); version=string(o,"version"); kind=string(o,"kind"); availability=string(o,"availability","remote")
        let fields=o["fields"] as? Object ?? [:]
        availableLength=string(fields,"Available Length");quality=string(fields,"Quality");trackLength=string(fields,"Track Length").isEmpty ? string(fields,"Length"):string(fields,"Track Length")
        sourceRow=number(o,"source_row") > 0 ? number(o,"source_row") : number(o,"row")
        sourceCount=number(o,"sourceCount")
        ambiguous=(o["ambiguous"] as? Bool) ?? (number(o,"ambiguous") != 0)
        eligible=(o["eligible"] as? Bool) ?? (number(o,"eligible") != 0)
    }
}
struct Worksheet: Identifiable {
    let name: String, count: Int
    var id: String { name }
    init(_ o: Object) { name=string(o,"name");count=number(o,"count") }
}
enum WorksheetPalette {
    static let colors:[String:String] = {
        guard let url=Bundle.main.resourceURL?.appendingPathComponent("Engine/WorksheetThemes.json"),let data=try? Data(contentsOf:url),let colors=try? JSONSerialization.jsonObject(with:data) as? [String:String] else{return [:]}
        return colors
    }()
}
enum EraPalette {
    static let colors:[String:Object] = {
        guard let url=Bundle.main.resourceURL?.appendingPathComponent("Engine/EraThemes.json"),let data=try? Data(contentsOf:url),let themes=try? JSONSerialization.jsonObject(with:data) as? [String:Object] else{return [:]}
        return themes
    }()
}
struct Era: Identifiable {
    let name: String, count: Int, description: String, timeline: String, artID: String, color: String, background: String
    var id: String { name }
    init(_ o: Object) {
        name=string(o,"name");count=number(o,"count")
        let meta=o["metadata"] as? Object ?? [:], art=o["artwork"] as? Object ?? [:], colors=meta["colors"] as? Object ?? EraPalette.colors[name] ?? [:]
        description=string(meta,"description"); timeline=string(meta,"timeline")
        artID=string(art,"rowId");color=string(colors,"accent","DFB77F");background=string(colors,"background",string(EraPalette.colors[name] ?? [:],"background","27262A"))
    }
}

func decodedArtwork(_ path:String) -> NSImage? {
    guard let source=CGImageSourceCreateWithURL(URL(fileURLWithPath:path) as CFURL,nil) else{return nil}
    let options:[CFString:Any]=[kCGImageSourceCreateThumbnailFromImageAlways:true,kCGImageSourceThumbnailMaxPixelSize:512,kCGImageSourceCreateThumbnailWithTransform:true,kCGImageSourceShouldCacheImmediately:true]
    guard let image=CGImageSourceCreateThumbnailAtIndex(source,0,options as CFDictionary) else{return nil}
    return NSImage(cgImage:image,size:NSSize(width:image.width,height:image.height))
}

@MainActor final class BundledArtwork {
    let directory:URL
    var eras:[String:Object]=[:], assets:[String:Object]=[:]
    private var images:[String:NSImage]=[:]
    init() {
        directory=Bundle.main.resourceURL!.appendingPathComponent("Engine/Artwork")
        if let data=try? Data(contentsOf:directory.appendingPathComponent("manifest.json")),let manifest=(try? JSONSerialization.jsonObject(with:data)) as? Object {
            eras=manifest["eras"] as? [String:Object] ?? [:];assets=manifest["assets"] as? [String:Object] ?? [:]
        }
    }
    func selection(_ era:String)->Object {eras[era] ?? [:]}
    func image(id:String)->NSImage? {
        if let image=images[id]{return image}
        guard let asset=assets[id] else{return nil}
        let file=string(asset,"file")
        guard !file.isEmpty,!file.contains("/"),!file.contains(".."),let image=decodedArtwork(directory.appendingPathComponent(file).path) else{return nil}
        images[id]=image;return image
    }
    func image(era:String)->NSImage? {image(id:string(selection(era),"assetId"))}
}

@MainActor final class Worker {
    var process: Process?
    var input: FileHandle?
    var pending: [String: CheckedContinuation<Any, Error>] = [:]
    var deadlines: [String: Task<Void, Never>] = [:]
    var stopping=false
    let reader=DispatchQueue(label:"local.tracker.next.reader",qos:.userInitiated)
    var errorHandler: ((String)->Void)?
    func start() throws {
        guard process == nil else { return }
        let resources=Bundle.main.resourceURL!
        let args=ProcessInfo.processInfo.arguments
        let reviewIndex=args.firstIndex(of:"--review-root")
        let root:URL
        if let index=reviewIndex,args.indices.contains(index+1),args[index+1].hasPrefix("/") {
            root=URL(fileURLWithPath:args[index+1],isDirectory:true).resolvingSymlinksInPath()
        }else{root=FileManager.default.urls(for:.applicationSupportDirectory,in:.userDomainMask)[0].appendingPathComponent("Tracker Player Next")}
        try FileManager.default.createDirectory(at:root,withIntermediateDirectories:true)
        let log=root.appendingPathComponent("worker.log")
        if !FileManager.default.fileExists(atPath:log.path) { FileManager.default.createFile(atPath:log.path,contents:nil) }
        let logHandle=try FileHandle(forWritingTo:log);try logHandle.seekToEnd()
        let child=Process(), stdin=Pipe(), stdout=Pipe()
        let python=Bundle.main.bundleURL.appendingPathComponent("Contents/Frameworks/Python3.framework/Versions/3.9")
        child.executableURL=python.appendingPathComponent("Resources/Python.app/Contents/MacOS/Python")
        child.arguments=[resources.appendingPathComponent("Engine/engine.py").path]
        var env=ProcessInfo.processInfo.environment.filter { !$0.key.hasPrefix("PYTHON") && !$0.key.hasPrefix("DYLD_") && $0.key != "__PYVENV_LAUNCHER__" }
        env["PYTHONHOME"]=python.path
        env["PYTHONPATH"]=resources.appendingPathComponent("Runtime").path
        env["PYTHONNOUSERSITE"]="1";env["PYTHONDONTWRITEBYTECODE"]="1";env["PYTHONUNBUFFERED"]="1";env["TRACKER_NEXT_ROOT"]=root.path
        child.environment=env;child.standardInput=stdin;child.standardOutput=stdout;child.standardError=logHandle
        child.terminationHandler={ [weak self] p in Task { @MainActor in
            guard let self else {return}
            let error=NSError(domain:"Tracker",code:Int(p.terminationStatus),userInfo:[NSLocalizedDescriptionKey:"The library worker stopped. Reopen the app. Its data is preserved; diagnostic log: \(log.path)"])
            for continuation in self.pending.values {continuation.resume(throwing:error)}
            self.pending.removeAll();self.deadlines.values.forEach{$0.cancel()};self.deadlines.removeAll()
            if !self.stopping {self.errorHandler?(error.localizedDescription)}
        }}
        try child.run();process=child;input=stdin.fileHandleForWriting
        let output=stdout.fileHandleForReading
        reader.async { [weak self] in
            var buffer=Data()
            while true {
                let data=output.availableData
                if data.isEmpty {break}
                buffer.append(data)
                while let newline=buffer.firstIndex(of:10) {
                    let line=buffer.prefix(upTo:newline);buffer.removeSubrange(...newline)
                    guard let msg=(try? JSONSerialization.jsonObject(with:line)) as? Object else {continue}
                    Task { @MainActor in self?.receive(msg) }
                }
            }
        }
    }
    func receive(_ message: Object) {
        let id=string(message,"request")
        guard let cont=pending.removeValue(forKey:id) else {return}
        deadlines.removeValue(forKey:id)?.cancel()
        if let error=message["error"] as? Object {
            cont.resume(throwing:NSError(domain:string(error,"code"),code:1,userInfo:[NSLocalizedDescriptionKey:string(error,"message")]))
        } else {cont.resume(returning:message["result"] ?? [:])}
    }
    func call(_ command: String,_ params: Object=[:],timeout:UInt64=240) async throws -> Any {
        guard let input,process?.isRunning==true else {throw NSError(domain:"Tracker",code:1,userInfo:[NSLocalizedDescriptionKey:"The local library is not running."])}
        let id=UUID().uuidString
        let data=try JSONSerialization.data(withJSONObject:["request":id,"command":command,"params":params]) + Data([10])
        return try await withCheckedThrowingContinuation { continuation in
            pending[id]=continuation
            deadlines[id]=Task { [weak self] in
                do {try await Task.sleep(nanoseconds:timeout * 1_000_000_000)} catch {return}
                guard let self else{return}
                self.deadlines.removeValue(forKey:id)
                self.pending.removeValue(forKey:id)?.resume(throwing:NSError(domain:"Tracker",code:2,userInfo:[NSLocalizedDescriptionKey:"The local library did not finish this action in time. Check Transfers before retrying a save."]))
            }
            do {try input.write(contentsOf:data)} catch {
                deadlines.removeValue(forKey:id)?.cancel();pending.removeValue(forKey:id)?.resume(throwing:error)
            }
        }
    }
    func stop() async {
        stopping=true
        try? input?.write(contentsOf:Data("{\"command\":\"shutdown\"}\n".utf8));try? input?.close();input=nil
        guard let process else{return}
        for _ in 0..<120 {
            if !process.isRunning {return}
            try? await Task.sleep(nanoseconds:250_000_000)
        }
        if process.isRunning {process.terminate()}
        for _ in 0..<20 {
            if !process.isRunning {return}
            try? await Task.sleep(nanoseconds:250_000_000)
        }
        // Only this app's exact child is stopped. SQLite rolls back unfinished work;
        // incomplete transfer files are recovered as interrupted on the next launch.
        if process.isRunning {Darwin.kill(process.processIdentifier,SIGKILL)}
        for _ in 0..<20 {
            if !process.isRunning {return}
            try? await Task.sleep(nanoseconds:100_000_000)
        }
    }
}

@MainActor final class Library: ObservableObject {
    static let shared=Library()
    let reviewMode=ProcessInfo.processInfo.arguments.contains("--review-root")
    let worker=Worker(), player=AVPlayer()
    @Published var ready=false
    @Published var worksheets:[Worksheet]=[]
    @Published var eras:[Era]=[]
    @Published var tracks:[Track]=[]
    @Published var worksheet="Unreleased"
    @Published var era=""
    @Published var query=""
    @Published var filter="all"
    @Published var kind=""
    @Published var section="library"
    @Published var selection=Set<String>()
    @Published var detail:Object=[:]
    @Published var inspectedFilePath=""
    @Published var inspectedFileID=""
    @Published var inspector=false
    @Published var navigation=NavigationTrail()
    @Published var restoringNavigation=false
    var navigationGeneration=0
    var lastQueryEdit=Date.distantPast
    @Published var total=0
    @Published var catalogCount=0
    @Published var captured=""
    @Published var busy=false
    @Published var now:Track?
    @Published var nowArt=""
    @Published var isPlaying=false
    @Published var elapsed=0.0
    @Published var duration=0.0
    @Published var volume=0.8
    @Published var shuffle=false
    @Published var skipShort=false
    @Published var includeUnknown=true
    @Published var avoidRepeats=true
    var shuffleCycle:Set<String>=[]
    @Published var repeatMode="off"
    @Published var sourceMode="idle"
    @Published var playbackNotice="Choose a recording. Browsing stays independent."
    @Published var playbackScope="No playback scope yet"
    @Published var loadingMedia=false
    @Published var isVideo=false
    @Published var showVideo=false
    @Published var jobs:[Object]=[]
    @Published var exports:[Object]=[]
    @Published var stats:Object=[:]
    @Published var limits:Object=[:]
    @Published var covers:[String:NSImage]=[:]
    let bundledArtwork=BundledArtwork()
    @Published var notice=""
    @Published var failure=""
    var sizePromptActive=false
    var selectedDownloadIDs:[String] {tracks.filter{selection.contains($0.id) && sourceIndicator(sourceCount:$0.sourceCount,availability:$0.availability,ambiguous:$0.ambiguous,eligible:$0.eligible).canDownload}.map(\.id)}
    func downloadable(_ ids:Set<String>)->[String] {tracks.filter{ids.contains($0.id) && sourceIndicator(sourceCount:$0.sourceCount,availability:$0.availability,ambiguous:$0.ambiguous,eligible:$0.eligible).canDownload}.map(\.id)}
    @Published var root=""
    var playbackIDs:[String]=[]
    var currentPath=""
    var scopeRequest:Object=[:]
    var searchTask:Task<Void,Never>?
    var loadGeneration=0, selectionGeneration=0, playGeneration=0, eraGeneration=0
    @Published var healthScope="all"
    var observer:NSKeyValueObservation?, timeObserver:Any?, endObserver:Any?
    var artPending=Set<String>()
    var history:[String]=[]
    var tick=0
    init() {
        worker.errorHandler={ [weak self] message in self?.failure=message }
        timeObserver=player.addPeriodicTimeObserver(forInterval:CMTime(seconds:0.25,preferredTimescale:600),queue:.main) { [weak self] t in
            Task { @MainActor in
                guard let self else{return}
                if self.player.currentItem != nil { self.elapsed=t.seconds.isFinite ? t.seconds:0 }
                self.duration=self.player.currentItem?.duration.seconds.isFinite == true ? self.player.currentItem!.duration.seconds:0
                self.isPlaying=self.player.timeControlStatus == .playing
                self.tick+=1
                if self.tick%20==0 && self.ready { await self.saveSession() }
            }
        }
        endObserver=NotificationCenter.default.addObserver(forName:.AVPlayerItemDidPlayToEndTime,object:nil,queue:.main) { [weak self] notification in
            Task { @MainActor in
                guard let self,notification.object as? AVPlayerItem === self.player.currentItem else{return}
                if self.repeatMode=="one" {self.seek(0);self.player.play()} else {await self.advance(1)}
            }
        }
    }
    func object(_ command:String,_ params:Object=[:]) async throws -> Object {try await worker.call(command,params,timeout:command=="prepare" && params["sizeToken"] != nil ? 7260:240) as? Object ?? [:]}
    func confirmLargeFile(_ request:Object,preview:Bool) async -> Bool {
        guard !sizePromptActive else{return false}
        sizePromptActive=true;defer{sizePromptActive=false}
        let alert=NSAlert();alert.messageText="Download this large file?";alert.alertStyle = .warning
        let size=number(request,"totalBytes"),limit=number(request,"limitBytes"),approved=number(request,"requestedBytes")
        let description=size>0 ? "File size: \(gigabytes(size))\nCurrent per-file limit: \(gigabytes(limit))" : "The source did not provide a file size. It reached \(gigabytes(limit)).\nAllow another attempt up to \(gigabytes(approved))?"
        alert.informativeText=string(request,"title")+"\n\n"+description+"\n\n"+(preview ? "Yes downloads a temporary copy and then plays it. Temporary storage may exceed your usual cache budget for this file." : "Yes saves this file with a one-time exception to the file and batch limits.")+"\nYour default limits stay unchanged. No skips this download."
        alert.addButton(withTitle:"No");alert.addButton(withTitle:"Yes")
        if let window=NSApp.keyWindow ?? NSApp.windows.first(where:{$0.isVisible}) {
            return await withCheckedContinuation { continuation in alert.beginSheetModal(for:window){response in continuation.resume(returning:response == .alertSecondButtonReturn)} }
        }
        return alert.runModal() == .alertSecondButtonReturn
    }
    func begin() async {
        guard !ready else{return}
        do {
            try worker.start();let boot=try await object("boot")
            worksheets=objects(boot["workbooks"]).map(Worksheet.init);catalogCount=number(boot,"count");captured=string(boot,"captured");root=string(boot,"root");limits=boot["limits"] as? Object ?? [:]
            let session=boot["session"] as? Object ?? [:]
            worksheet=string(session,"workbook","Unreleased");era=string(session,"era");volume=min(1,max(0,session["volume"] as? Double ?? 0.8));player.volume=Float(volume)
            skipShort=session["skipShort"] as? Bool ?? false;includeUnknown=session["includeUnknown"] as? Bool ?? true;avoidRepeats=session["avoidRepeats"] as? Bool ?? true
            shuffle=session["shuffle"] as? Bool ?? false;repeatMode=string(session,"repeat","off")
            shuffleCycle=Set(session["shuffleCycle"] as? [String] ?? [])
            playbackIDs=session["scopeIDs"] as? [String] ?? [];playbackScope=string(session,"scopeLabel","Restored recording · choose a scope to continue")
            if let id=session["playing"] as? String,!id.isEmpty {
                let d=try await object("detail",["id":id]);if let r=d["row"] as? Object {now=Track(r)}
                elapsed=session["time"] as? Double ?? 0;playbackNotice="Restored at \(durationText(elapsed)). Press Play to resume."
                nowArt=string(d["artwork"] as? Object ?? [:],"rowId")
            }
            ready=true;await loadEras();await reload()
            if let selected=session["selected"] as? String,!selected.isEmpty {selection=[selected];await inspect(selected)}
            await refreshActivity()
        } catch {failure=error.localizedDescription}
    }
    var scope:Object { ["workbook":worksheet,"era":era,"query":query,"filter":filter,"kind":kind] }
    var eraInfo:Era? { eras.first{$0.name==era} }
    var navigationState:NavigationState {
        NavigationState(section:section,worksheet:worksheet,era:era,query:query,filter:filter,kind:kind,selection:selection,inspector:inspector,loaded:tracks.count,healthScope:healthScope)
    }
    var canGoBack:Bool {!navigation.states.isEmpty && !restoringNavigation}
    func rememberNavigation() {if ready && !restoringNavigation {navigation.record(navigationState);lastQueryEdit = .distantPast}}
    func navigateSection(_ value:String) {
        guard section != value else{return};rememberNavigation();section=value
        if value=="insights" {Task{await refreshStats()}}
        if value=="transfers" {Task{await refreshActivity()}}
    }
    func chooseWorksheet(_ name:String) {
        guard !restoringNavigation else{return}
        guard section != "library" || worksheet != name || !era.isEmpty || !query.isEmpty || filter != "all" || !kind.isEmpty else{return}
        rememberNavigation();section="library";worksheet=name;era="";query="";filter="all";kind="";selection=[];detail=[:];selectionGeneration+=1
        Task {await loadEras();await reload()}
    }
    func chooseEra(_ name:String) {
        guard era != name,!restoringNavigation else{return};rememberNavigation();era=name;selection=[];detail=[:];selectionGeneration+=1
        Task{await reload();if let e=eraInfo,!e.artID.isEmpty {await loadArt(e.artID)}}
    }
    func editQuery(_ value:String) {
        guard query != value else{return}
        if Date().timeIntervalSince(lastQueryEdit)>0.8 {rememberNavigation()}
        lastQueryEdit=Date();query=value
    }
    func chooseFilter(_ value:String) {guard filter != value else{return};rememberNavigation();filter=value}
    func chooseKind(_ value:String) {guard kind != value else{return};rememberNavigation();kind=value}
    func chooseHealthScope(_ value:String) {guard healthScope != value else{return};rememberNavigation();healthScope=value}
    func selectRows(_ ids:Set<String>) {
        guard selection != ids,!restoringNavigation else{return};rememberNavigation();selectionGeneration+=1;selection=ids
        if ids.isEmpty {detail=[:]}
    }
    func setInspector(_ value:Bool) {guard inspector != value else{return};rememberNavigation();inspector=value}
    func goBack() async {
        guard !restoringNavigation,let previous=navigation.pop(current:navigationState) else{return}
        restoringNavigation=true;navigationGeneration+=1;let generation=navigationGeneration
        defer{restoringNavigation=false}
        searchTask?.cancel();loadGeneration+=1;eraGeneration+=1;selectionGeneration+=1
        section=previous.section;worksheet=previous.worksheet;era=previous.era;query=previous.query;filter=previous.filter;kind=previous.kind
        inspector=previous.inspector;healthScope=previous.healthScope;selection=[];detail=[:]
        await loadEras();await reload()
        while generation==navigationGeneration,tracks.count<min(previous.loaded,total) {
            let count=tracks.count;await reload(more:true);if tracks.count<=count {break}
        }
        selection=previous.selection
        if let id=selection.first {await inspect(id)}
        if section=="insights" {await refreshStats()}
        if section=="transfers" {await refreshActivity()}
        await saveSession()
    }
    func eraBackground(_ name:String)->String {eras.first(where:{$0.name==name})?.background ?? "27262A"}
    func loadEras() async {
        eraGeneration+=1;let generation=eraGeneration;let selected=worksheet
        do{let rows=objects(try await worker.call("eras",["workbook":selected]));guard generation==eraGeneration,selected==worksheet else{return};eras=rows.map(Era.init)}catch{if generation==eraGeneration{failure=error.localizedDescription}}
    }
    func scheduleSearch() {
        searchTask?.cancel();searchTask=Task{try? await Task.sleep(nanoseconds:180_000_000);guard !Task.isCancelled else{return};await reload()}
    }
    func reload(more:Bool=false) async {
        loadGeneration+=1;let generation=loadGeneration;busy=true
        var p=scope;p["offset"]=more ? tracks.count:0;p["limit"]=500
        do {
            let response=try await object("query",p);guard generation==loadGeneration else{return}
            let result=objects(response["rows"]).map(Track.init)
            tracks=more ? tracks+result:result;total=number(response,"total")
            busy=false;await saveSession()
        }catch{if generation==loadGeneration{busy=false;failure=error.localizedDescription}}
    }
    func inspect(_ id:String) async {
        selectionGeneration+=1;let generation=selectionGeneration
        do{let d=try await object("detail",["id":id]);guard generation==selectionGeneration else{return};detail=d
            if let art=d["artwork"] as? Object {await loadArt(string(art,"rowId"))}
            await saveSession()
        }catch{failure=error.localizedDescription}
    }
    func loadArt(_ id:String) async {
        guard !id.isEmpty,covers[id]==nil else{return}
        if let image=bundledArtwork.image(id:id){covers[id]=image;return}
        guard (!reviewMode || id.hasPrefix("assigned-")),!artPending.contains(id),artPending.count<2 else{return}
        artPending.insert(id);defer{artPending.remove(id)}
        do{let result=try await object("art",["id":id,"localOnly":reviewMode]);if let image=decodedArtwork(string(result,"path")){covers[id]=image}}catch{/* The immutable era image remains visible when a remote cover fails. */}
    }
    func play(_ id:String,source:String?=nil,remote:Bool=false,captureScope:Bool=true,resume:Double=0) async {
        playGeneration+=1;let generation=playGeneration;loadingMedia=true
        var preparedPathToRelease:String?
        let requestedScope=scope,requestedLabel=worksheet+(era.isEmpty ? " · all eras":" · "+era)+(query.isEmpty ? "":" · “"+query+"”")
        do{
            let d=try await object("detail",["id":id]);guard generation==playGeneration else{return};guard let r=d["row"] as? Object else{loadingMedia=false;return}
            if reviewMode {
                let local=d["file"] as? Object ?? [:]
                guard string(local,"state")=="available",source==nil,!remote else{loadingMedia=false;failure="Review mode plays local fixture files only.";return}
            }
            var p:Object=["id":id,"remote":remote];if let source{p["source"]=source}
            var prepared=try await object("prepare",p)
            while let approval=prepared["approval"] as? Object {
                let token=string(approval,"token")
                guard generation==playGeneration else{_ = try? await object("discard_size",["token":token]);return}
                loadingMedia=false
                let allow=await confirmLargeFile(approval,preview:true)
                guard allow,generation==playGeneration else{_ = try? await object("discard_size",["token":token]);if generation==playGeneration{playbackNotice="Large download skipped. Current playback preserved."};return}
                p["sizeToken"]=token;loadingMedia=true
                prepared=try await object("prepare",p)
            }
            preparedPathToRelease=string(prepared,"path")
            guard generation==playGeneration else{_ = try? await worker.call("unpin",["path":string(prepared,"path")]);return}
            let path=string(prepared,"path"),kind=string(prepared,"kind")
            guard ["audio","video"].contains(kind) else {
                loadingMedia=false;notice="This source is \(kind). Use Reveal Inspected File to inspect the asset in Finder."
                detail=d;inspectedFilePath=path;inspectedFileID=id;selection=[id];inspector=true
                _ = try? await worker.call("unpin",["path":path]);return
            }
            if captureScope {
                let ids=try await worker.call("ids",requestedScope.merging(["playableOnly":true]){_,new in new}) as? [String] ?? []
                guard generation==playGeneration else{_ = try? await worker.call("unpin",["path":path]);return}
                playbackIDs=ids.contains(id) ? ids:[id]+ids
                playbackScope=requestedLabel+" · \(playbackIDs.count) rows";scopeRequest=requestedScope;history=[];shuffleCycle=[]
            }
            guard generation==playGeneration else{_ = try? await worker.call("unpin",["path":path]);return}
            player.pause();if !currentPath.isEmpty {let oldPath=currentPath;Task{_ = try? await worker.call("unpin",["path":oldPath])}}
            currentPath=path;preparedPathToRelease=nil;now=Track(r);shuffleCycle.insert(id);nowArt=string(d["artwork"] as? Object ?? [:],"rowId")
            sourceMode=string(prepared,"mode");playbackNotice=string(prepared,"notice");isVideo=kind=="video";showVideo=isVideo
            let item=AVPlayerItem(url:URL(fileURLWithPath:path));player.replaceCurrentItem(with:item)
            observer=item.observe(\.status,options:[.new,.initial]) { [weak self] item,_ in Task { @MainActor in
                guard let self,self.player.currentItem===item else{return}
                if item.status == .readyToPlay {
                    self.loadingMedia=false
                    if resume>0 {self.player.seek(to:CMTime(seconds:resume,preferredTimescale:600))}
                    self.player.play()
                } else if item.status == .failed {
                    self.loadingMedia=false;self.playbackNotice="This file could not be decoded by macOS. Choose another source or open it externally."
                    self.failure=item.error?.localizedDescription ?? "Unsupported media codec."
                }
            }}
            if !nowArt.isEmpty {Task{await loadArt(nowArt)}}
            await saveSession()
        }catch{if let path=preparedPathToRelease{_ = try? await worker.call("unpin",["path":path])};if generation==playGeneration{loadingMedia=false;failure=error.localizedDescription;playbackNotice="Source unavailable. Current playback was preserved; choose another source explicitly."}}
    }
    func toggle() {
        if loadingMedia{return}
        if player.currentItem != nil {isPlaying ? player.pause():player.play()}
        else if let now {Task{await play(now.id,captureScope:false,resume:elapsed)}}
        else if let id=selection.first {Task{await play(id)}}
    }
    func advance(_ direction:Int) async {
        guard !playbackIDs.isEmpty else{notice="Choose a recording from a worksheet to establish a playback scope.";return}
        var id:String?
        if direction<0,elapsed>3 {seek(0);return}
        if direction<0 {guard !history.isEmpty else{seek(0);return};id=history.removeLast()}
        else if shuffle {
            let generation=playGeneration,current=now?.id
            do {
                let result=try await object("shuffle_candidates",["ids":playbackIDs,"skipShort":skipShort,"includeUnknown":includeUnknown])
                guard generation==playGeneration,current==now?.id else{return}
                let eligible=result["ids"] as? [String] ?? []
                let choice=shuffleChoices(eligible:eligible,current:current,played:shuffleCycle,avoidRepeats:avoidRepeats,repeatAll:repeatMode=="all")
                if choice.newCycle{shuffleCycle=[]}
                id=choice.ids.randomElement()
                if id==nil {player.pause();notice="No more songs match your shuffle settings in this playback scope.";return}
            } catch {failure=error.localizedDescription;return}
        }
        else if let index=playbackIDs.firstIndex(of:now?.id ?? "") {
            let next=index+direction
            if playbackIDs.indices.contains(next){id=playbackIDs[next]}
            else if repeatMode=="all"{id=direction>0 ? playbackIDs.first:playbackIDs.last}
        }else{id=playbackIDs.first}
        guard let id else{player.pause();notice="End of the captured playback scope.";return}
        if direction>0,let now{history.append(now.id)}
        await play(id,captureScope:false)
    }
    func seek(_ time:Double) {player.seek(to:CMTime(seconds:time,preferredTimescale:600));elapsed=time}
    func stop() {playGeneration+=1;loadingMedia=false;player.pause();player.replaceCurrentItem(with:nil);isPlaying=false;sourceMode="idle";playbackNotice="Stopped. Recording and position retained.";if !currentPath.isEmpty{let path=currentPath;currentPath="";Task{_ = try? await worker.call("unpin",["path":path])}}}
    func revealPlaying() async {
        guard let now else{return}
        rememberNavigation()
        worksheet=now.workbook;era=now.era;query="";filter="all";kind="";section="library";selection=[now.id]
        await loadEras();await reload();await inspect(now.id)
        while worksheet==now.workbook,era==now.era,!tracks.contains(where:{$0.id==now.id}),tracks.count<total {
            let previousCount=tracks.count;await reload(more:true)
            if tracks.count<=previousCount {break}
        }
    }
    func saveSession() async {
        guard ready else{return}
        let p:Object=["workbook":worksheet,"era":era,"selected":selection.first ?? "","playing":now?.id ?? "","time":elapsed,"volume":volume,"scopeIDs":playbackIDs,"scopeLabel":playbackScope,"shuffle":shuffle,"skipShort":skipShort,"includeUnknown":includeUnknown,"avoidRepeats":avoidRepeats,"shuffleCycle":Array(shuffleCycle),"repeat":repeatMode]
        _ = try? await worker.call("session",p,timeout:5)
    }
    func download(_ ids:[String]) async {
        guard !reviewMode else{failure="Review mode does not download remote media.";return}
        do{_ = try await worker.call("enqueue",["ids":ids]);navigateSection("transfers");await refreshActivity()}catch{failure=error.localizedDescription}
    }
    func downloadScope() async {
        guard !reviewMode else{failure="Review mode does not download remote media.";return}
        do {
            let result=try await object("download_all",["workbook":worksheet,"era":era]);navigateSection("transfers")
            notice="Queued \((result["jobs"] as? [String] ?? []).count) rows in this era. Transfers continue automatically. Missing links create text placeholders; unresolved access waits for your choice. Per-file approvals and free-space checks still apply."
            await refreshActivity()
        }catch{failure=error.localizedDescription}
    }
    func refreshActivity() async {
        do{
            let a=try await object("activity");jobs=objects(a["jobs"]);exports=objects(a["exports"]);limits=a["limits"] as? Object ?? [:];deliveryBatches=objects(a["batches"])
            if !sizePromptActive,let job=jobs.first(where:{string($0,"state")=="awaiting_approval"}),let approval=job["approval"] as? Object {
                let allow=await confirmLargeFile(approval,preview:false)
                _ = try await object("size_decision",["id":string(job,"id"),"allow":allow])
            }
        }catch{failure=error.localizedDescription}
    }
    func refreshStats() async {do{stats=try await object("stats",healthScope=="all" ? [:]:healthScope=="worksheet" ? ["workbook":worksheet]:["workbook":worksheet,"era":era])}catch{failure=error.localizedDescription}}
    func perform(_ command:String,_ p:Object=[:]) async {
        do{
            let result=try await object(command,p)
            if let path=result["path"] as? String{NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath:path)])}
            if command=="verify" {notice="\(number(result,"verified")) files verified; \(number(result,"missingOrChanged")) missing or changed."}
            if command=="detach" {notice="Association detached. Original file and history preserved."}
            await refreshActivity();await refreshStats();await reload()
            if let id=selection.first{await inspect(id)}
        }catch{failure=error.localizedDescription}
    }
    func relink(_ id:String) {
        let panel=NSOpenPanel();panel.canChooseDirectories=false;panel.allowsMultipleSelection=false;panel.message="Choose the exact downloaded file. Its checksum must match this source row."
        if panel.runModal() == .OK,let url=panel.url{Task{await perform("relink",["id":id,"path":url.path])}}
    }
    func openSource(_ value:String) {
        guard let url=URL(string:value),["https","http"].contains(url.scheme?.lowercased() ?? ""),url.user==nil,url.password==nil else{failure="This source link is not a safe HTTP(S) URL.";return}
        NSWorkspace.shared.open(url)
    }
    @Published var deliveryBatches:[Object]=[]
    @Published var recoveryJob:Object=[:]
    @Published var recoveryInfo:Object=[:]
    @Published var showRecovery=false
    func recoverSource(_ job:Object) async {
        do{recoveryInfo=try await object("source_recovery",["id":string(job,"id")]);recoveryJob=job;showRecovery=true}
        catch{failure=error.localizedDescription}
    }
    func attachDownload(_ job:Object) {
        let panel=NSOpenPanel();panel.canChooseDirectories=false;panel.allowsMultipleSelection=false
        panel.message="Choose the file you downloaded for \(string(job,"title")). You confirm its identity; this app cannot verify your browser session. The selected original is preserved."
        guard panel.runModal() == .OK,let url=panel.url else{return}
        Task {
            let size=(try? url.resourceValues(forKeys:[.fileSizeKey]).fileSize) ?? 0
            var allowLarge=false
            if size>number(limits,"fileMB")*1048576 {
                allowLarge=await confirmLargeFile(["title":string(job,"title"),"totalBytes":size,"limitBytes":number(limits,"fileMB")*1048576,"requestedBytes":size],preview:false)
                if !allowLarge{return}
            }
            await perform("attach_download",["id":string(job,"id"),"path":url.path,"allowLarge":allowLarge]);showRecovery=false
        }
    }
    func assignArtwork(rowID:String,group:Bool) {
        let panel=NSOpenPanel();panel.canChooseDirectories=false;panel.allowsMultipleSelection=false;panel.allowedContentTypes=[.jpeg,.png]
        panel.message=group && worksheet=="Released" ? "Choose the official album cover for this Released era. Selecting it identifies it as official." : group ? "Choose a unique cover for this worksheet-era group. Covers already assigned to another non-Released group are rejected." : "Choose artwork for this exact song. It takes precedence over era artwork."
        guard panel.runModal() == .OK,let url=panel.url,decodedArtwork(url.path) != nil else{return}
        Task {await perform("assign_art",["rowId":rowID,"path":url.path,"scope":group ? "group":"row","official":group && worksheet=="Released"]);await loadEras()}
    }
    func reveal(_ path:String) {guard !path.isEmpty else{return};NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath:path)])}
}

struct Cover: View {
    @ObservedObject var model:Library
    var id:String="",era:String="",size:CGFloat=64,color:Color=Color(hex:"DFB77F")
    var image:NSImage? {model.covers[id] ?? model.bundledArtwork.image(id:id) ?? nil}
    var caption:String {image == nil ? "Artwork assignment needed":id.hasPrefix("assigned-") ? "User-assigned artwork":model.covers[id] != nil && !id.hasPrefix("bundle-") ? "Recording artwork from its exact tracker source":string(model.bundledArtwork.selection(era),"caption","Artwork")}
    var body:some View {
        ZStack {
            RoundedRectangle(cornerRadius:10).fill(Color.black.opacity(0.65))
            if let image {Image(nsImage:image).resizable().scaledToFit()}
            else {Image(systemName:"waveform").font(.system(size:size*0.32,weight:.light)).foregroundStyle(color.opacity(0.7))}
        }.frame(width:size,height:size).clipShape(RoundedRectangle(cornerRadius:10))
        .overlay(RoundedRectangle(cornerRadius:10).stroke(Color.white.opacity(0.1),lineWidth:1))
        .accessibilityLabel(image == nil ? "Artwork assignment needed":caption).help(caption)
        .task(id:id){await model.loadArt(id)}
    }
}

struct EraCard:View {
    @ObservedObject var model:Library
    let era:Era
    var body:some View {
        Button{model.chooseEra(era.name)}label:{
            VStack(alignment:.leading,spacing:9){
                Cover(model:model,id:era.artID,era:era.name,size:142,color:Color(hex:era.color))
                Text(era.name).font(.system(size:13,weight:.semibold)).lineLimit(2).multilineTextAlignment(.leading).frame(height:34,alignment:.topLeading)
                Text("\(era.count.formatted()) rows  ·  Explore →").font(.system(size:10)).foregroundStyle(.secondary)
            }.padding(12).frame(width:166,height:226).background(Color(hex:era.background).opacity(0.45),in:RoundedRectangle(cornerRadius:12))
        }.buttonStyle(.plain).help(era.name+" — "+string(model.bundledArtwork.selection(era.name),"caption"))
    }
}

struct ArtworkAttribution:View {
    @ObservedObject var model:Library
    let era:String
    var body:some View {
        let art=model.eras.first(where:{$0.name==era}).map{["caption":$0.artID.isEmpty ? "Artwork assignment needed":"Assigned artwork", "note":$0.artID.isEmpty ? "Choose a song, then assign an official Released cover or a unique worksheet-era cover in Source details.":"Artwork uses the exact row or worksheet-era assignment."]} ?? [:]
        if !art.isEmpty {
            DisclosureGroup("Era artwork") {
                VStack(alignment:.leading,spacing:7){
                    Text(string(art,"caption")).font(.caption.bold())
                    Text(string(art,"note")).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    Button("View artwork source"){model.openSource(string(art,"sourceUrl"))}.font(.caption)
                }.frame(maxWidth:.infinity,alignment:.leading).padding(.top,6)
            }.font(.caption)
        }
    }
}
struct Eyebrow:View {
    var text:String
    var body:some View{Text(text.uppercased()).font(.system(size:10,weight:.bold,design:.rounded)).tracking(1.8).foregroundStyle(.secondary)}
}
struct Pill:View {
    var text:String,color:Color = .secondary
    var body:some View{Text(text).font(.system(size:10,weight:.semibold)).foregroundStyle(color).padding(.horizontal,8).padding(.vertical,4).background(color.opacity(0.12),in:Capsule())}
}

struct ContentView:View {
    @StateObject var model=Library.shared
    @FocusState var searchFocused:Bool
    @State var showLimits=false
    var body:some View {
        VStack(spacing:0) {
            HStack(spacing:0) {
                sidebar.frame(width:210)
                Divider()
                VStack(spacing:0) {
                    toolbar
                    Divider()
                    if !model.ready {ProgressView("Opening indexed library…").frame(maxWidth:.infinity,maxHeight:.infinity)}
                    else if model.section=="transfers" {transfers}
                    else if model.section=="insights" {insights}
                    else {library}
                }.frame(maxWidth:.infinity,maxHeight:.infinity)
            }
            Divider()
            bottomPanel
        }
        .frame(minWidth:1140,minHeight:690)
        .background(Color(nsColor:.windowBackgroundColor))
        .preferredColorScheme(.dark)
        .tint(Color(hex:"DBB782"))
        .task {await model.begin()}
        .task {while !Task.isCancelled {try? await Task.sleep(nanoseconds:2_000_000_000);if model.ready && !model.sizePromptActive {await model.refreshActivity()}}}
        .onChange(of:model.query){_,_ in if !model.restoringNavigation {model.scheduleSearch()}}
        .onChange(of:model.filter){_,_ in if !model.restoringNavigation {Task{await model.reload()}}}
        .onChange(of:model.kind){_,_ in if !model.restoringNavigation {Task{await model.reload()}}}
        .onChange(of:model.selection){_,ids in if let id=ids.first {Task{await model.inspect(id)}}}
        .onReceive(NotificationCenter.default.publisher(for:.init("TrackerSearch"))){_ in searchFocused=true;model.navigateSection("library")}
        .alert("Action could not complete",isPresented:Binding(get:{!model.failure.isEmpty},set:{if !$0{model.failure=""}})){Button("OK"){model.failure=""}} message:{Text(model.failure)}
        .overlay(alignment:.top) {if !model.notice.isEmpty {HStack{Text(model.notice).font(.callout);Button{model.notice=""}label:{Image(systemName:"xmark.circle.fill")}.buttonStyle(.plain)}.padding(12).background(.regularMaterial,in:RoundedRectangle(cornerRadius:12)).padding(.top,12).shadow(radius:8)}}
        .sheet(isPresented:$showLimits){LimitsView(model:model)}
        .sheet(isPresented:$model.showRecovery){RecoveryView(model:model)}
        .sheet(isPresented:$model.showVideo){VStack{HStack{Text(model.now?.title ?? "Video").font(.headline);Spacer();Button("Back"){model.showVideo=false}.keyboardShortcut(.cancelAction)};VideoPlayer(player:model.player).frame(minWidth:720,minHeight:405)}.padding(16)}
    }
    var sidebar:some View {
        VStack(alignment:.leading,spacing:0) {
            HStack(spacing:10){Image(systemName:"square.stack.3d.up.fill").font(.title2).foregroundStyle(Color(hex:"DBB782"));VStack(alignment:.leading,spacing:2){Text("TRACKER").font(.system(size:18,weight:.heavy,design:.rounded)).tracking(2);Text("KANYE WEST").font(.system(size:9,weight:.medium)).tracking(2).foregroundStyle(.secondary)}}.padding(22)
            VStack(spacing:4){nav("library","Library","square.grid.2x2");nav("transfers","Transfers & Exports","arrow.down.circle");nav("insights","Library Health","chart.bar.xaxis")}.padding(.horizontal,12)
            HStack{Eyebrow(text:"Worksheets");Spacer();Text("\(model.worksheets.count)").font(.caption).foregroundStyle(.tertiary)}.padding(.horizontal,22).padding(.top,28).padding(.bottom,10)
            ScrollView {LazyVStack(spacing:3){ForEach(model.worksheets){w in Button{model.chooseWorksheet(w.name)}label:{HStack{RoundedRectangle(cornerRadius:2).fill(Color(hex:WorksheetPalette.colors[w.name] ?? "777777")).frame(width:5,height:22).overlay(RoundedRectangle(cornerRadius:2).stroke(Color.white.opacity(0.25),lineWidth:0.5));Text(w.name).lineLimit(1);Spacer(minLength:4);Text(w.count.formatted()).font(.system(size:11,design:.monospaced)).foregroundStyle(.secondary)}.font(.system(size:12,weight:model.worksheet==w.name ? .semibold:.regular)).padding(.horizontal,12).padding(.vertical,9).background(Color(hex:WorksheetPalette.colors[w.name] ?? "27262A").opacity(model.worksheet==w.name && model.section=="library" ? 0.48:0.14),in:RoundedRectangle(cornerRadius:7))}.buttonStyle(.plain).help(w.name)}}.padding(.horizontal,12)}
            VStack(alignment:.leading,spacing:6){Label("Local library",systemImage:"lock.shield").font(.caption);Text("\(model.catalogCount.formatted()) source rows\nSnapshot · \(model.captured)").font(.system(size:10)).foregroundStyle(.secondary)}.padding(22)
        }.background(Color.black.opacity(0.14))
    }
    func nav(_ section:String,_ title:String,_ icon:String)->some View {
        Button{model.navigateSection(section)}label:{HStack{Image(systemName:icon).frame(width:20);Text(title);Spacer()}.font(.system(size:12,weight:.medium)).padding(10).background(model.section==section ? Color(hex:"DBB782").opacity(0.13):.clear,in:RoundedRectangle(cornerRadius:8)).foregroundStyle(model.section==section ? Color(hex:"E9CAA0"):.primary)}.buttonStyle(.plain)
    }
    var toolbar:some View {
        HStack(spacing:14) {
            Button{Task{await model.goBack()}}label:{Label("Back",systemImage:"chevron.left")}.disabled(!model.canGoBack).help("Return to the previous screen").keyboardShortcut("[",modifiers:.command)
            Text(model.section=="library" ? model.worksheet:model.section=="transfers" ? "Transfers & Exports":"Library Health").font(.system(size:15,weight:.semibold))
            Spacer()
            if model.section=="library" {
                HStack{Image(systemName:"magnifyingglass").foregroundStyle(.secondary);TextField("Search names, versions, source fields",text:Binding(get:{model.query},set:{model.editQuery($0)})).textFieldStyle(.plain).focused($searchFocused);if !model.query.isEmpty{Button{model.editQuery("")}label:{Image(systemName:"xmark.circle.fill")}.buttonStyle(.plain)}}.padding(8).background(Color.black.opacity(0.2),in:RoundedRectangle(cornerRadius:8)).frame(maxWidth:340)
            }
            Button{showLimits=true}label:{Image(systemName:"gearshape")}.help("Settings").accessibilityLabel("Settings").keyboardShortcut(",",modifiers:.command)
            Button{model.reveal(model.root)}label:{Image(systemName:"folder")}.help("Reveal app data")
        }.buttonStyle(.borderless).padding(.horizontal,22).frame(height:60).background(model.section=="library" ? Color(hex:WorksheetPalette.colors[model.worksheet] ?? "27262A").opacity(0.24):.clear)
    }
    var library:some View {
        VStack(spacing:0) {
            if model.era.isEmpty && model.query.isEmpty {
                eraShelf
            }else if let e=model.eraInfo {
                HStack(spacing:18){Cover(model:model,id:e.artID,era:e.name,size:76,color:Color(hex:e.color));VStack(alignment:.leading,spacing:6){Button{model.chooseEra("")}label:{Label("All eras",systemImage:"chevron.left").font(.caption)}.buttonStyle(.plain).foregroundStyle(.secondary);Text(e.name).font(.system(size:26,weight:.bold));Text(e.timeline.isEmpty ? "\(e.count.formatted()) source rows · original tracker order":e.timeline).font(.caption).foregroundStyle(.secondary).lineLimit(2)};Spacer();Button{Task{await model.downloadScope()}}label:{Label("Download All",systemImage:"arrow.down.circle.fill")}.help("Download every row in this era automatically, including placeholders for unavailable recordings. Per-file limits still apply.")}.padding(22).background(LinearGradient(colors:[Color(hex:e.background).opacity(0.6),Color.clear],startPoint:.leading,endPoint:.trailing))
                ArtworkAttribution(model:model,era:e.name).padding(.horizontal,22).padding(.bottom,8)
                if !e.description.isEmpty {
                    VStack(alignment:.leading,spacing:6){Eyebrow(text:"Era notes");ScrollView{Text(e.description).font(.callout).foregroundStyle(.secondary).textSelection(.enabled).frame(maxWidth:.infinity,alignment:.leading)}}.frame(maxHeight:100).padding(.horizontal,22).padding(.bottom,12)
                }
            }
            HStack(spacing:12){Text("\(model.total.formatted()) results").font(.system(size:12,weight:.semibold)).monospacedDigit();if model.busy{ProgressView().controlSize(.small)};Spacer();Picker("Media",selection:Binding(get:{model.kind},set:{model.chooseKind($0)})){Text("All assets").tag("");Text("Audio").tag("audio");Text("Video").tag("video");Text("Other").tag("other")}.labelsHidden().frame(width:110);Picker("Availability",selection:Binding(get:{model.filter},set:{model.chooseFilter($0)})){Text("All states").tag("all");Text("Local files").tag("local");Text("Missing / changed").tag("missing");Text("Ambiguous identity").tag("ambiguous");Text("Playable rows").tag("playable")}.labelsHidden().frame(width:150);if !model.selectedDownloadIDs.isEmpty{Button{Task{await model.download(model.selectedDownloadIDs)}}label:{Label("Save \(model.selectedDownloadIDs.count)",systemImage:"arrow.down.circle")}}}.padding(.horizontal,22).padding(.vertical,12)
            if model.tracks.isEmpty && !model.busy {ContentUnavailableViewCompat(title:"No matching rows",detail:"Try a different era, search or availability filter.",symbol:"magnifyingglass")}
            else {
                List(selection:Binding(get:{model.selection},set:{model.selectRows($0)})) {
                    ForEach(model.tracks) {track in
                        SongRow(model:model,track:track)
                            .tag(track.id)
                            .listRowBackground(Color(hex:model.eraBackground(track.era)))
                            .listRowSeparator(.hidden)
                    }
                }.listStyle(.plain).scrollContentBackground(.hidden)
                .contextMenu(forSelectionType:String.self){ids in
                    if let id=ids.first {
                        Button("Play"){Task{await model.play(id)}}
                        Button("Inspect source"){model.selectRows([id]);model.setInspector(true)}
                        let downloadable=model.downloadable(ids)
                        if !downloadable.isEmpty {Button("Save selected"){Task{await model.download(downloadable)}}}
                    }
                } primaryAction:{ids in if let id=ids.first{Task{await model.play(id)}}}
                if model.tracks.count<model.total {Button("Load next \(min(500,model.total-model.tracks.count)) rows · \(model.tracks.count.formatted()) loaded of \(model.total.formatted())"){Task{await model.reload(more:true)}}.buttonStyle(.borderless).padding(10)}
            }
        }
    }
    var eraShelf:some View {
        VStack(alignment:.leading,spacing:12){HStack{VStack(alignment:.leading,spacing:5){Eyebrow(text:"Browse the source");Text("Every era. Every version.").font(.system(size:25,weight:.bold))};Spacer();Text("\(model.eras.count) eras").font(.caption).foregroundStyle(.secondary)}
            ScrollView(.horizontal,showsIndicators:false){
                LazyHStack(spacing:12){ForEach(model.eras){e in
                    if model.inspector {
                        Button{model.chooseEra(e.name)}label:{HStack(spacing:10){Cover(model:model,id:e.artID,era:e.name,size:42);VStack(alignment:.leading,spacing:4){Text(e.name).font(.caption.bold()).lineLimit(2);Text("\(e.count) rows").font(.system(size:10)).foregroundStyle(.secondary)}}.frame(width:190,alignment:.leading).padding(10).background(Color(hex:e.background).opacity(0.35),in:RoundedRectangle(cornerRadius:10))}.buttonStyle(.plain)
                    } else {EraCard(model:model,era:e)}
                }}
            }

        }.padding(22)
    }
    var bottomPanel:some View {
        VStack(spacing:0) {
            HStack(spacing:12) {
                Label("Source & playback",systemImage:"music.note.list").font(.caption.bold())
                let selected=model.detail["row"] as? Object ?? [:]
                if !selected.isEmpty {
                    Text("Selected: "+string(selected,"title")).font(.caption).lineLimit(1)
                    if string(selected,"id") != model.now?.id {Button("Play selected"){Task{await model.play(string(selected,"id"))}}.controlSize(.small)}
                } else {Text("Select a song to inspect its source").font(.caption).foregroundStyle(.secondary)}
                Spacer()
                Button{model.setInspector(!model.inspector)}label:{Label(model.inspector ? "Hide details":"Source details",systemImage:model.inspector ? "chevron.down":"chevron.up")}.controlSize(.small)
            }.padding(.horizontal,22).padding(.vertical,9)
            if model.inspector {
                Divider()
                Inspector(model:model).id(string(model.detail["row"] as? Object ?? [:],"id")).frame(height:220)
            }
            Divider()
            playerBar
        }.background(Color.black.opacity(0.2))
    }
    var playerBar:some View {
        HStack(spacing:18){Button{Task{await model.revealPlaying()}}label:{HStack(spacing:12){Cover(model:model,id:model.nowArt,era:model.now?.era ?? "",size:52);VStack(alignment:.leading,spacing:4){Text(model.now?.title ?? "Nothing playing").font(.system(size:13,weight:.semibold)).lineLimit(1);Text(model.now?.era ?? "Choose a recording to begin").font(.caption).foregroundStyle(.secondary).lineLimit(1)}}.frame(width:250,alignment:.leading)}.buttonStyle(.plain).help("Reveal the playing source row")
            VStack(spacing:9){HStack(spacing:18){Button{model.shuffle.toggle()}label:{Image(systemName:"shuffle").foregroundStyle(model.shuffle ? Color(hex:"DBB782"):.secondary)}.help("Shuffle captured scope").accessibilityLabel("Shuffle captured scope");Button{Task{await model.advance(-1)}}label:{Image(systemName:"backward.end.fill")}.help("Previous recording").accessibilityLabel("Previous recording");Button{model.toggle()}label:{ZStack{Circle().fill(Color(hex:"E9CAA0")).frame(width:34,height:34);if model.loadingMedia{ProgressView().controlSize(.small)}else{Image(systemName:model.isPlaying ? "pause.fill":"play.fill").foregroundStyle(.black)}}}.accessibilityLabel(model.isPlaying ? "Pause":"Play");Button{Task{await model.advance(1)}}label:{Image(systemName:"forward.end.fill")}.help("Next recording").accessibilityLabel("Next recording");Button{model.repeatMode=model.repeatMode=="off" ? "all":model.repeatMode=="all" ? "one":"off"}label:{Image(systemName:model.repeatMode=="one" ? "repeat.1":"repeat").foregroundStyle(model.repeatMode=="off" ? .secondary:Color(hex:"DBB782"))}.help("Repeat: \(model.repeatMode)")}
                HStack(spacing:9){Text(durationText(model.elapsed)).frame(width:40,alignment:.trailing);Slider(value:Binding(get:{min(model.elapsed,max(model.duration,1))},set:{model.seek($0)}),in:0...max(model.duration,1)).disabled(model.duration<=0).accessibilityLabel("Playback position");Text(durationText(model.duration)).frame(width:40,alignment:.leading)}.font(.system(size:10,design:.monospaced)).foregroundStyle(.secondary)
            }.frame(maxWidth:.infinity)
            VStack(alignment:.trailing,spacing:8){HStack(spacing:10){if model.isVideo{Button{model.showVideo=true}label:{Image(systemName:"video")}.help("Show video")};Pill(text:model.sourceMode=="permanent" ? "VERIFIED LOCAL":model.sourceMode=="temporary" ? "TEMPORARY":model.sourceMode=="external" ? "VERIFIED EXTERNAL":"PAUSED",color:model.sourceMode=="permanent" ? .green:Color(hex:"DBB782"));Image(systemName:"speaker.wave.2");Slider(value:$model.volume,in:0...1).frame(width:75).onChange(of:model.volume){_,v in model.player.volume=Float(v)}.accessibilityLabel("Volume")};Text(model.playbackScope).font(.system(size:10)).foregroundStyle(.secondary).lineLimit(1).help(model.playbackScope)}.frame(width:265)
        }.buttonStyle(.plain).padding(.horizontal,22).padding(.vertical,14).background(Color.black.opacity(0.22)).help(model.playbackNotice)
    }
    var transfers:some View {
        ScrollView{VStack(alignment:.leading,spacing:24){HStack{VStack(alignment:.leading,spacing:6){Eyebrow(text:"On this Mac");Text("Keep what matters.").font(.system(size:30,weight:.bold));Text("Original downloads and separate import packages, with source identity intact.").foregroundStyle(.secondary)};Spacer();Button("Settings…"){showLimits=true};Button("Cancel Active"){Task{await model.perform("cancel")}}.disabled(!model.jobs.contains{["queued","running","awaiting_approval","awaiting_access"].contains(string($0,"state"))})}
            HStack(spacing:12){Pill(text:"2 concurrent transfers");Pill(text:"\(number(model.limits,"count")) rows per manual batch");Pill(text:"\(gigabytes(number(model.limits,"fileMB")*1048576)) per file");Pill(text:"\(gigabytes(number(model.limits,"batchMB")*1048576)) per batch")}
            Eyebrow(text:"Downloads")
            if model.jobs.isEmpty {ContentUnavailableViewCompat(title:"No transfers yet",detail:"Select exact source rows in the library, then choose Save.",symbol:"arrow.down.circle")}
            ForEach(model.deliveryBatches,id:\.selfID){batch in EraRunProgress(model:model,batch:batch)}
            ForEach(model.jobs,id:\.selfID){job in TransferJobView(model:model,job:job)}
            HStack{Eyebrow(text:"Import-ready packages");Spacer();Button("Export Library Manifest"){Task{await model.perform("manifest")}}}
            Text("Packages preserve originals and include exact row/version provenance. Apple Music is never changed automatically.").font(.caption).foregroundStyle(.secondary)
            ForEach(model.exports,id:\.selfID){e in HStack{Image(systemName:"shippingbox");VStack(alignment:.leading){Text(string(e,"title"));Text(timestamp(string(e,"created"))).font(.caption).foregroundStyle(.secondary)};Spacer();Button("Show Package"){model.reveal(string(e,"path"))}}.padding(14).background(Color.white.opacity(0.035),in:RoundedRectangle(cornerRadius:10))}
        }.padding(28)}
    }
    var insights:some View {
        ScrollView{VStack(alignment:.leading,spacing:24){Eyebrow(text:"Trust the counts");Text("A library with receipts.").font(.system(size:32,weight:.bold));Text("Coverage is measured in source rows. Overlapping worksheets and alternate versions are never counted as unique songs.").foregroundStyle(.secondary)
            Picker("Coverage scope",selection:Binding(get:{model.healthScope},set:{model.chooseHealthScope($0)})){Text("Whole catalog").tag("all");Text(model.worksheet).tag("worksheet");if !model.era.isEmpty{Text(model.era).tag("era")}}.pickerStyle(.segmented).onChange(of:model.healthScope){_,_ in Task{await model.refreshStats()}}
            LazyVGrid(columns:[GridItem(.adaptive(minimum:170))],spacing:14){metric("Source rows",number(model.stats,"rows"),"Captured catalog");metric("Download history",number(model.stats,"downloaded"),"Recorded original saves");metric("Available files",number(model.stats,"available"),"Last verified file state");metric("Ambiguous rows",number(model.stats,"ambiguous"),"Kept separate for review");metric("Missing or changed",number(model.stats,"missing"),"Relink by checksum")}
            VStack(alignment:.leading,spacing:14){LabeledContent("Source revision",value:model.captured);LabeledContent("Last successful download",value:timestamp(string(model.stats,"last_download","None")));LabeledContent("Last file verification",value:timestamp(string(model.stats,"last_verified","None")));LabeledContent("Database integrity",value:string(model.stats,"integrity","Not checked"));LabeledContent("Temporary cache",value:gigabytes(number(model.stats,"cacheBytes")));Text("Local counts describe recorded verification, not a continuous disk scan. Verify known files after moving data. Provider availability is only established by actual playback or download attempts.").font(.caption).foregroundStyle(.secondary)}.padding(22).background(Color.white.opacity(0.035),in:RoundedRectangle(cornerRadius:14))
            HStack{Button("Verify Known Files"){Task{await model.perform("verify")}};Button("Show App Data"){model.reveal(model.root)};Button("Export Manifest"){Task{await model.perform("manifest")}}}
        }.padding(30)}
    }
    func metric(_ title:String,_ count:Int,_ caption:String)->some View {VStack(alignment:.leading,spacing:10){Text(title).font(.caption).foregroundStyle(.secondary);Text(count.formatted()).font(.system(size:34,weight:.semibold,design:.rounded));Text(caption).font(.system(size:10)).foregroundStyle(.secondary)}.frame(maxWidth:.infinity,alignment:.leading).padding(20).background(Color.white.opacity(0.04),in:RoundedRectangle(cornerRadius:14))}
}

extension Dictionary where Key==String,Value==Any {var selfID:String {self["id"] as? String ?? ""}}
struct ContentUnavailableViewCompat:View {
    var title:String,detail:String,symbol:String
    var body:some View{VStack(spacing:13){Image(systemName:symbol).font(.system(size:34,weight:.light)).foregroundStyle(.secondary);Text(title).font(.title3.bold());Text(detail).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)}.frame(maxWidth:.infinity,maxHeight:.infinity).padding(40)}
}

struct SongRow:View {
    @ObservedObject var model:Library
    let track:Track
    var fill:String {model.eraBackground(track.era)}
    var ink:Color {eraUsesDarkText(fill) ? .black:.white}
    var source:SourceIndicator {sourceIndicator(sourceCount:track.sourceCount,availability:track.availability,ambiguous:track.ambiguous,eligible:track.eligible)}
    func tag(_ text:String,icon:String,label:String)->some View {Label(text,systemImage:icon).font(.system(size:10,weight:.semibold)).lineLimit(1).padding(.horizontal,8).padding(.vertical,4).background(ink.opacity(0.10),in:Capsule()).help(label+": "+text).accessibilityLabel(label+": "+text)}
    var body:some View {
        HStack(spacing:14) {
            Image(systemName:model.now?.id==track.id && model.isPlaying ? "waveform":track.kind=="video" ? "film":track.kind=="audio" ? "music.note":"doc").frame(width:22)
            VStack(alignment:.leading,spacing:4) {
                Text(track.title).font(.system(size:13,weight:.semibold)).lineLimit(2)
                Text(track.era+" · source row "+String(track.sourceRow)).font(.system(size:10)).lineLimit(1)
            }.frame(maxWidth:.infinity,alignment:.leading)
            VStack(alignment:.leading,spacing:5) {
                tag(track.availableLength.isEmpty ? "Not specified":track.availableLength,icon:"waveform",label:"Available length")
                tag(track.quality.isEmpty ? "Not specified":track.quality,icon:"slider.horizontal.3",label:"Source quality")
            }.frame(width:158,alignment:.leading)
            Label(track.trackLength.isEmpty ? "—":track.trackLength,systemImage:"clock").font(.system(size:11,weight:.medium,design:.monospaced)).lineLimit(2).frame(width:84,alignment:.leading).help("Tracker duration: \(track.trackLength.isEmpty ? "unknown":track.trackLength)")
            if let icon=source.icon {Image(systemName:icon).font(.system(size:16)).frame(width:24).help(source.label).accessibilityLabel(source.label)}
            Image(systemName:model.selection.contains(track.id) ? "checkmark.circle.fill":"circle").opacity(model.selection.contains(track.id) ? 1:0.35).frame(width:18)
        }.foregroundStyle(ink).padding(.horizontal,14).padding(.vertical,9)
        .frame(maxWidth:.infinity,alignment:.leading).background(Color(hex:fill))
        .overlay(Rectangle().strokeBorder(model.selection.contains(track.id) ? ink.opacity(0.85):.clear,lineWidth:2))
        .help(track.name).accessibilityElement(children:.combine)
    }
}

struct Inspector:View {
    @ObservedObject var model:Library
    var row:Object{model.detail["row"] as? Object ?? [:]}
    var file:Object{model.detail["file"] as? Object ?? [:]}
    var fields:Object{row["fields"] as? Object ?? [:]}
    var artID:String{string(model.detail["artwork"] as? Object ?? [:],"rowId")}
    var body:some View {
        Group {
            if row.isEmpty {ContentUnavailableViewCompat(title:"Select a song",detail:"Its source, artwork and tracker details will appear here beside the playback controls.",symbol:"doc.text.magnifyingglass")}
            else {
                HStack(alignment:.top,spacing:0) {
                    ScrollView {identity.padding(18)}.frame(maxWidth:.infinity,maxHeight:.infinity)
                    Divider()
                    ScrollView {sources.padding(18)}.frame(maxWidth:.infinity,maxHeight:.infinity)
                    Divider()
                    ScrollView {metadata.padding(18)}.frame(maxWidth:.infinity,maxHeight:.infinity)
                }
            }
        }.background(Color.black.opacity(0.12))
    }
    var identity:some View {
        VStack(alignment:.leading,spacing:12) {
            Eyebrow(text:"Selected source")
            HStack(alignment:.top,spacing:12) {
                Cover(model:model,id:artID,era:string(row,"era"),size:64)
                VStack(alignment:.leading,spacing:5) {
                    Text(string(row,"title")).font(.headline).fixedSize(horizontal:false,vertical:true).textSelection(.enabled)
                    Text(string(row,"era")).font(.caption).foregroundStyle(.secondary)
                }
            }
            ArtworkAttribution(model:model,era:string(row,"era"))
            HStack {
                if !(row["links"] as? [String] ?? []).isEmpty,(row["eligible"] as? Bool) == true,(row["ambiguous"] as? Bool) != true {
                    Button("Save selected source"){Task{await model.download([string(row,"id")])}}
                }
                Button("Open tracker row"){model.openSource(string(row,"sourceUrl"))}
            }.controlSize(.small)
            Text(string(row,"name")).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            Text(string(model.detail["artwork"] as? Object ?? [:],"reason")).font(.caption).foregroundStyle(.orange)
            HStack{Button("Assign song art…"){model.assignArtwork(rowID:string(row,"id"),group:false)};Button("Assign era cover…"){model.assignArtwork(rowID:string(row,"id"),group:true)}}.controlSize(.small)
            if row["ambiguous"] as? Bool == true {Label("Ambiguous identity — inspect before saving.",systemImage:"exclamationmark.triangle").font(.caption).foregroundStyle(.orange)}
            if !file.isEmpty {
                Divider();Eyebrow(text:"Local file")
                Text(string(file,"path")).font(.system(size:10)).textSelection(.enabled)
                HStack {
                    Button("Reveal"){model.reveal(string(file,"path"))}
                    Button("Relink…"){model.relink(string(row,"id"))}
                    Button("Detach"){Task{await model.perform("detach",["id":string(row,"id")])}}
                }.controlSize(.small)
                Button("Prepare Music Import Package"){Task{await model.perform("export",["id":string(row,"id")])}}.disabled(string(row,"kind") != "audio")
            }
        }.frame(maxWidth:.infinity,alignment:.leading)
    }
    var sources:some View {
        VStack(alignment:.leading,spacing:12) {
            Eyebrow(text:"Source links")
            let links=row["links"] as? [String] ?? []
            if links.isEmpty {Text("No media link supplied. Tracker details remain available.").font(.caption).foregroundStyle(.secondary)}
            ForEach(Array(links.enumerated()),id:\.offset) {index,link in
                VStack(alignment:.leading,spacing:7) {
                    Text("Source \(index+1) · \(URL(string:link)?.host ?? "Unknown host")").font(.caption.bold())
                    Text(link).font(.system(size:10,design:.monospaced)).lineLimit(2).textSelection(.enabled).help(link)
                    HStack {Button("Play this source"){Task{await model.play(string(row,"id"),source:link)}};Button("Open link"){model.openSource(link)}}.controlSize(.small)
                }.padding(10).background(Color.white.opacity(0.04),in:RoundedRectangle(cornerRadius:8))
            }
            if model.inspectedFileID==string(row,"id"),!model.inspectedFilePath.isEmpty {Button("Reveal inspected file"){model.reveal(model.inspectedFilePath)}}
            ForEach(Array(objects(model.detail["attempts"]).enumerated()),id:\.offset){_,attempt in if !string(attempt,"error").isEmpty{Text(string(attempt,"error")).font(.caption).foregroundStyle(.orange)}}
        }.frame(maxWidth:.infinity,alignment:.leading)
    }
    var metadata:some View {
        VStack(alignment:.leading,spacing:12) {
            Eyebrow(text:"Tracker details")
            ForEach(fields.keys.sorted(),id:\.self) {key in
                VStack(alignment:.leading,spacing:4) {
                    Text(key).font(.system(size:10,weight:.semibold)).foregroundStyle(.secondary)
                    let value=String(describing:fields[key] ?? "")
                    if value.count>240 {DisclosureGroup {Text(value).font(.caption).textSelection(.enabled)} label:{Text(String(value.prefix(100))+"…").font(.caption).lineLimit(2)}}
                    else {Text(value.isEmpty ? "—":value).font(.caption).textSelection(.enabled)}
                }
            }
            Divider()
            Text("\(string(row,"workbook")) · source row \(number(row,"row"))\nIdentity: \(string(row,"id"))\nRevision: \(String(string(row,"sourceHash").prefix(16)))").font(.system(size:10,design:.monospaced)).foregroundStyle(.secondary).textSelection(.enabled)
        }.frame(maxWidth:.infinity,alignment:.leading)
    }
}

struct LimitsView:View {
    @ObservedObject var model:Library
    @Environment(\.dismiss) var dismiss
    @State var shuffle=false
    @State var skipShort=false
    @State var includeUnknown=true
    @State var avoidRepeats=true
    @State var count=25
    @State var file=0.134217728
    @State var batch=0.536870912
    @State var cache=0.268435456
    @State var preview=0.067108864
    func mib(_ value:Double)->Int {value.isFinite && value>0 && value<100 ? max(1,Int((value*1_000_000_000/1048576).rounded())):0}
    var body:some View {
        VStack(alignment:.leading,spacing:20) {
            Text("Settings").font(.title2.bold())
            VStack(alignment:.leading,spacing:10){
                Text("Shuffle").font(.headline)
                Toggle("Shuffle playback",isOn:$shuffle)
                Toggle("Exclude songs shorter than 30 seconds",isOn:$skipShort)
                Toggle("Include songs with unknown duration",isOn:$includeUnknown)
                Toggle("Avoid repeats until the scope is finished",isOn:$avoidRepeats)
                Text("Uses the tracker’s duration across the entire playback scope. Exactly 30 seconds is included. Unclear or missing lengths count as unknown. Changes apply to the next shuffled song; Repeat All starts another cycle.").font(.caption).foregroundStyle(.secondary)
            }
            Divider()
            Text("Transfer limits").font(.headline)
            Text("Sizes use GB (1 GB = 1 billion bytes). Larger files ask for Yes or No; approving one file leaves these defaults unchanged.").font(.callout).foregroundStyle(.secondary)
            Form {
                TextField("Rows per batch (1–250)",value:$count,format:.number)
                TextField("GB per saved file (up to 1.074)",value:$file,format:.number.precision(.fractionLength(3...6)))
                TextField("GB per batch (up to 4.295)",value:$batch,format:.number.precision(.fractionLength(3...6)))
                TextField("GB per Play download (up to 0.134)",value:$preview,format:.number.precision(.fractionLength(3...6)))
                TextField("GB temporary cache (up to 1.074)",value:$cache,format:.number.precision(.fractionLength(3...6)))
            }
            HStack {
                Button("Back"){dismiss()}.keyboardShortcut(.cancelAction);Spacer()
                Button("Save Settings") {Task {
                    do {model.limits=try await model.object("settings",["count":count,"fileMB":mib(file),"batchMB":mib(batch),"cacheMB":mib(cache),"previewMB":mib(preview)]);model.shuffle=shuffle;model.skipShort=skipShort;model.includeUnknown=includeUnknown;model.avoidRepeats=avoidRepeats;await model.saveSession();dismiss()}
                    catch {model.failure=error.localizedDescription}
                }}.keyboardShortcut(.defaultAction)
            }
        }.padding(28).frame(width:560).onAppear {
            shuffle=model.shuffle;skipShort=model.skipShort;includeUnknown=model.includeUnknown;avoidRepeats=model.avoidRepeats
            count=number(model.limits,"count");file=Double(number(model.limits,"fileMB"))*1048576/1_000_000_000
            batch=Double(number(model.limits,"batchMB"))*1048576/1_000_000_000;cache=Double(number(model.limits,"cacheMB"))*1048576/1_000_000_000
            preview=Double(number(model.limits,"previewMB"))*1048576/1_000_000_000
        }
    }

}

final class AppDelegate:NSObject,NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool{true}
    func applicationShouldTerminate(_ sender:NSApplication)->NSApplication.TerminateReply {
        Task { @MainActor in await Library.shared.saveSession();Library.shared.player.pause();await Library.shared.worker.stop();NSApp.reply(toApplicationShouldTerminate:true) }
        return .terminateLater
    }
}
@main struct TrackerPlayerApp:App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body:some Scene {
        Window("Tracker Player Next",id:"main") {ContentView()}.defaultSize(width:1440,height:920)
            .commands {
                CommandGroup(replacing:.newItem){}
                CommandGroup(after:.sidebar){Button("Search Library"){NotificationCenter.default.post(name:.init("TrackerSearch"),object:nil)}.keyboardShortcut("f",modifiers:.command);Button("Toggle Source Inspector"){Library.shared.setInspector(!Library.shared.inspector)}.keyboardShortcut("i",modifiers:[.command,.option])}
                CommandMenu("Playback"){
                    Button("Play / Pause"){Library.shared.toggle()}.keyboardShortcut(.space,modifiers:[])
                    Button("Next Recording"){Task{await Library.shared.advance(1)}}.keyboardShortcut(.rightArrow,modifiers:.command)
                    Button("Previous Recording"){Task{await Library.shared.advance(-1)}}.keyboardShortcut(.leftArrow,modifiers:.command)
                    Button("Stop"){Library.shared.stop()}.keyboardShortcut(".",modifiers:.command)
                    Divider();Button("Reveal Playing"){Task{await Library.shared.revealPlaying()}}.keyboardShortcut("l",modifiers:.command)
                }
                CommandMenu("Library"){
                    Button("Save Selected Rows"){Task{await Library.shared.download(Library.shared.selectedDownloadIDs)}}.keyboardShortcut("d",modifiers:.command).disabled(Library.shared.selectedDownloadIDs.isEmpty)
                    Button("Transfers & Exports"){Library.shared.navigateSection("transfers")}.keyboardShortcut("j",modifiers:.command)
                    Button("Show App Data"){Library.shared.reveal(Library.shared.root)}
                }
            }
    }
}
