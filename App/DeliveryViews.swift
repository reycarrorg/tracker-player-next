import SwiftUI

struct RecoveryView:View {
    @ObservedObject var model:Library
    var body:some View {
        VStack(alignment:.leading,spacing:18){
            Text("Resolve source access").font(.title2.bold())
            Text(string(model.recoveryJob,"title")).font(.headline)
            Text(string(model.recoveryInfo,"message")).fixedSize(horizontal:false,vertical:true)
            Text("No passwords, cookies, or browser-session data are collected. A public retry does not imply that browser sign-in succeeded.").font(.caption).foregroundStyle(.secondary)
            HStack{Button("Open provider / Sign in"){model.openSource(string(model.recoveryInfo,"url"))};Button("Retry same public source"){Task{await model.perform("retry",["id":string(model.recoveryJob,"id")]);model.showRecovery=false}}}
            Button("Attach downloaded file…"){model.attachDownload(model.recoveryJob)}
            Divider()
            HStack{Button("Back · keep unresolved"){model.showRecovery=false};Spacer();Button("Cannot resolve · create placeholder"){Task{await model.perform("mark_unresolved",["id":string(model.recoveryJob,"id")]);model.showRecovery=false}}}
        }.padding(26).frame(width:620)
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
            Text("\(done) of \(number(batch,"total")) rows resolved or stopped").font(.headline)
            ProgressView(value:Double(done),total:Double(max(number(batch,"total"),1)))
            Text("\(number(counts,"completed")) saved · \(number(counts,"skipped")) already saved · \(number(counts,"placeholder")) placeholders · \(number(counts,"failed")+number(counts,"metadata_failed")) failed · \(number(counts,"awaiting_access")) need access · \(number(counts,"cancelled")+number(counts,"interrupted")) stopped").font(.caption)
            Button("Retry stopped / unresolved rows"){Task{await model.perform("retry_batch",["batch":string(batch,"batch")])}}.controlSize(.small)
        }.padding(16).background(Color.white.opacity(0.04),in:RoundedRectangle(cornerRadius:10))
    }
}
