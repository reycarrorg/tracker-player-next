import SwiftUI

struct RecoveryView:View {
    @ObservedObject var model:Library
    @State private var browserVisible=false
    var browserURL:URL? {URL(string:string(model.recoveryInfo,"url"))}
    var body:some View {
        VStack(alignment:.leading,spacing:18){
            HStack{Button(browserVisible ? "Back to choices":"Back · keep unresolved"){if browserVisible{browserVisible=false}else{model.showRecovery=false}};Spacer();Text("Secure provider session").font(.caption.bold()).foregroundStyle(.secondary)}
            if browserVisible,let url=browserURL {
                VStack(alignment:.leading,spacing:10){
                    Text(string(model.recoveryJob,"title")).font(.headline)
                    Text("Sign in yourself, then use the provider’s download button. WebKit keeps this site session inside Tracker Player for later downloads.").font(.callout)
                    AuthenticatedBrowser(sourceURL:url,completed:{file in model.attachBrowserDownload(model.recoveryJob,file:file)},failed:{message in model.failure=message})
                        .frame(minWidth:900,minHeight:570)
                }
            } else {
                Text("Resolve source access").font(.title2.bold())
                Text(string(model.recoveryJob,"title")).font(.headline)
                Text(string(model.recoveryInfo,"message")).fixedSize(horizontal:false,vertical:true)
                Text("Your password and WebKit session stay out of transfer logs and the Python engine. Tracker Player receives only the completed file you chose to download.").font(.caption).foregroundStyle(.secondary)
                HStack{Button("Sign in and download in app"){browserVisible=true}.buttonStyle(.borderedProminent);Button("Open in default browser"){model.openSource(string(model.recoveryInfo,"url"))}}
                HStack{Button("Retry public download"){Task{await model.perform("retry",["id":string(model.recoveryJob,"id")]);model.showRecovery=false}};Button("Attach a file already downloaded…"){model.attachDownload(model.recoveryJob)}}
            }
            Divider()
            HStack{Spacer();Button("Cannot resolve · create placeholder"){Task{await model.perform("mark_unresolved",["id":string(model.recoveryJob,"id")]);model.showRecovery=false}}}
        }.padding(26).frame(minWidth:browserVisible ? 950:620)
    }
}

struct TransferJobView:View {
    @ObservedObject var model:Library
    let job:Object
    var state:String{string(job,"state")}
    var body:some View {
        HStack(alignment:.top,spacing:14){
            Image(systemName:state=="placeholder" ? "doc.text":state=="awaiting_access" ? "person.crop.circle.badge.exclamationmark":["completed","skipped"].contains(state) ? "checkmark.circle.fill":"arrow.down.circle").font(.title2)
            VStack(alignment:.leading,spacing:6){
                Text(string(job,"title")).font(.headline)
                Text(string(job,"workbook")+" / "+string(job,"era")).font(.caption).foregroundStyle(.secondary)
                if ["running","queued","cancelling"].contains(state){ProgressView(value:Double(number(job,"bytes")),total:Double(max(number(job,"total"),1)))}
                Text(string(job,"error")).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                if ["awaiting_access","failed","placeholder","interrupted","cancelled"].contains(state){
                    HStack{Button("Resolve / Sign in…"){Task{await model.recoverSource(job)}};Button("Placeholder"){Task{await model.perform("mark_unresolved",["id":string(job,"id")])}}}.controlSize(.small)
                }
                if ["failed","cancelled","interrupted","placeholder","awaiting_access","metadata_failed"].contains(state){Button("Retry this row"){Task{await model.perform("retry",["id":string(job,"id")])}}}
                if ["completed","skipped","metadata_failed"].contains(state){Button("Save a copy…"){model.saveCopy(job)}.controlSize(.small)}
            }.frame(maxWidth:.infinity,alignment:.leading)
            VStack(alignment:.trailing,spacing:6){Pill(text:state.replacingOccurrences(of:"_",with:" ").uppercased());Text(gigabytes(number(job,"bytes"))+" / "+(number(job,"total")>0 ? gigabytes(number(job,"total")):"size unknown")).font(.caption).monospacedDigit()}
        }.padding(16).background(Color.white.opacity(0.035),in:RoundedRectangle(cornerRadius:12))
    }
}

struct EraRunProgress:View {
    @ObservedObject var model:Library
    let batch:Object
    var counts:Object{batch["counts"] as? Object ?? [:]}
    var done:Int{["completed","skipped","placeholder","failed","metadata_failed","cancelled","interrupted"].reduce(0){$0+number(counts,$1)}}
    var body:some View {
        VStack(alignment:.leading,spacing:8){
            Text(string(batch,"label","Selected source rows")).font(.title3.bold())
            Text("\(done) of \(number(batch,"total")) rows resolved or stopped").font(.headline)
            ProgressView(value:Double(done),total:Double(max(number(batch,"total"),1)))
            Text("\(number(counts,"completed")) saved · \(number(counts,"skipped")) already saved · \(number(counts,"placeholder")) placeholders · \(number(counts,"failed")+number(counts,"metadata_failed")) failed · \(number(counts,"awaiting_access")) need access · \(number(counts,"cancelled")+number(counts,"interrupted")) stopped").font(.caption)
            Button("Retry stopped / unresolved rows"){Task{await model.perform("retry_batch",["batch":string(batch,"batch")])}}.controlSize(.small)
        }.padding(16).background(Color.white.opacity(0.04),in:RoundedRectangle(cornerRadius:10))
    }
}
