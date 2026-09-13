import Foundation

struct SourceIndicator:Equatable {
    let icon:String?
    let label:String
    let canDownload:Bool
}

func sourceIndicator(sourceCount:Int,availability:String,ambiguous:Bool,eligible:Bool)->SourceIndicator {
    if availability=="available" {return SourceIndicator(icon:"checkmark.icloud",label:"Saved locally",canDownload:false)}
    if availability=="missing" || availability=="changed" {return SourceIndicator(icon:"exclamationmark.icloud",label:"Local file missing or changed",canDownload:sourceCount>0)}
    if sourceCount==0 {return SourceIndicator(icon:nil,label:"No source link",canDownload:false)}
    if availability=="authentication_required" {return SourceIndicator(icon:"person.crop.circle.badge.exclamationmark",label:"Provider sign-in required",canDownload:false)}
    if availability=="access_unavailable" {return SourceIndicator(icon:"lock.icloud",label:"Source access unavailable",canDownload:false)}
    if availability=="network_failure" {return SourceIndicator(icon:"arrow.triangle.2.circlepath.icloud",label:"Temporary network failure",canDownload:false)}
    if availability=="broken_source" {return SourceIndicator(icon:"icloud.slash",label:"Broken source link",canDownload:false)}
    if ambiguous {return SourceIndicator(icon:"exclamationmark.triangle",label:"Source needs review",canDownload:false)}
    if eligible {return SourceIndicator(icon:"icloud.and.arrow.down",label:"Remote source · download to play",canDownload:true)}
    return SourceIndicator(icon:"icloud.slash",label:"Source unavailable",canDownload:false)
}

struct NavigationState: Equatable {
    var section:String, worksheet:String, era:String, query:String, filter:String, kind:String
    var selection:Set<String>, inspector:Bool, loaded:Int, healthScope:String
}

struct NavigationTrail {
    private(set) var states:[NavigationState]=[]
    mutating func record(_ state:NavigationState) {
        guard states.last != state else{return}
        states.append(state)
        if states.count>100 {states.removeFirst(states.count-100)}
    }
    mutating func pop(current:NavigationState)->NavigationState? {
        while let state=states.popLast() {if state != current{return state}}
        return nil
    }
}

// WCAG relative luminance chooses the more legible foreground on the exact era fill.
func eraUsesDarkText(_ hex:String)->Bool {
    let n=UInt64(hex.trimmingCharacters(in:CharacterSet(charactersIn:"#")),radix:16) ?? 0x27262A
    func linear(_ channel:UInt64)->Double {
        let s=Double(channel)/255
        return s<=0.04045 ? s/12.92:pow((s+0.055)/1.055,2.4)
    }
    let luminance=0.2126*linear((n>>16)&255)+0.7152*linear((n>>8)&255)+0.0722*linear(n&255)
    return (luminance+0.05)/0.05 >= 1.05/(luminance+0.05)
}

// Track IDs come from the captured scope, after the backend duration filter.
func shuffleChoices(eligible:[String],current:String?,played:Set<String>,avoidRepeats:Bool,repeatAll:Bool)->(ids:[String],newCycle:Bool) {
    let unseen=eligible.filter{$0 != current && (!avoidRepeats || !played.contains($0))}
    if !unseen.isEmpty || !repeatAll{return (unseen,false)}
    let nextCycle=eligible.filter{$0 != current}
    return (nextCycle.isEmpty ? eligible:nextCycle,true)
}
