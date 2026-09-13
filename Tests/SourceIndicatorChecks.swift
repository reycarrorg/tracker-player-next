import Foundation

@main struct SourceIndicatorChecks {
    static func main() {
        let absent=sourceIndicator(sourceCount:0,availability:"remote",ambiguous:false,eligible:false)
        precondition(absent.icon == nil && !absent.canDownload && absent.label == "No source link")
        let remote=sourceIndicator(sourceCount:1,availability:"remote",ambiguous:false,eligible:true)
        precondition(remote.icon == "icloud.and.arrow.down" && remote.canDownload)
        let local=sourceIndicator(sourceCount:0,availability:"available",ambiguous:false,eligible:false)
        precondition(local.icon == "checkmark.icloud" && !local.canDownload)
        precondition(sourceIndicator(sourceCount:1,availability:"authentication_required",ambiguous:false,eligible:false).label == "Provider sign-in required")
        precondition(sourceIndicator(sourceCount:1,availability:"access_unavailable",ambiguous:false,eligible:false).label == "Source access unavailable")
        precondition(sourceIndicator(sourceCount:1,availability:"network_failure",ambiguous:false,eligible:false).label == "Temporary network failure")
        precondition(sourceIndicator(sourceCount:1,availability:"broken_source",ambiguous:false,eligible:false).label == "Broken source link")
        print("Source indicator checks passed.")
    }
}
