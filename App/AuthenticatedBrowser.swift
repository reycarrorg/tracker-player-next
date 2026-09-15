import SwiftUI
import WebKit

struct AuthenticatedBrowser: NSViewRepresentable {
    let sourceURL: URL
    let completed: (URL) -> Void
    let failed: (String) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(completed: completed, failed: failed) }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        // The persistent store is private to this app. Cookies and credentials are
        // never copied into the Python download engine or diagnostic records.
        configuration.websiteDataStore = .default()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        context.coordinator.webView = webView
        webView.load(URLRequest(url: sourceURL, cachePolicy: .reloadRevalidatingCacheData))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {}

    @MainActor final class Coordinator: NSObject, WKNavigationDelegate, WKDownloadDelegate {
        weak var webView: WKWebView?
        let completed: (URL) -> Void
        let failed: (String) -> Void
        private var destinations: [ObjectIdentifier: URL] = [:]

        init(completed: @escaping (URL) -> Void, failed: @escaping (String) -> Void) {
            self.completed = completed; self.failed = failed
        }

        private func allowed(_ url: URL?) -> Bool {
            guard let url, url.scheme?.lowercased() == "https", url.user == nil, url.password == nil,
                  url.port == nil || url.port == 443, let host = url.host?.lowercased(), !host.isEmpty else { return false }
            if host == "localhost" || host.hasSuffix(".localhost") || host.hasSuffix(".local") { return false }
            // Literal IP destinations are unnecessary for tracker providers and can
            // expose local services. Public provider domain names remain available.
            if host.contains(":") || host.allSatisfy({ $0.isNumber || $0 == "." }) { return false }
            return true
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     preferences: WKWebpagePreferences,
                     decisionHandler: @escaping (WKNavigationActionPolicy, WKWebpagePreferences) -> Void) {
            guard allowed(navigationAction.request.url) else {
                failed("Tracker Player blocked a non-public or non-HTTPS browser destination.")
                decisionHandler(.cancel, preferences); return
            }
            decisionHandler(navigationAction.shouldPerformDownload ? .download : .allow, preferences)
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                     decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            guard allowed(navigationResponse.response.url) else {
                failed("Tracker Player blocked a non-public or non-HTTPS download response.")
                decisionHandler(.cancel); return
            }
            decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download)
        }

        func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
            download.delegate = self
        }

        func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
            download.delegate = self
        }

        func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse,
                      newRequest request: URLRequest,
                      decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void) {
            decisionHandler(allowed(request.url) ? .allow : .cancel)
        }

        func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                      suggestedFilename: String,
                      completionHandler: @escaping (URL?) -> Void) {
            let cleanName = suggestedFilename.replacingOccurrences(of: "/", with: "_")
                .replacingOccurrences(of: ":", with: "_")
            let panel = NSSavePanel()
            panel.title = "Save authenticated download"
            panel.message = "Choose where WebKit should save this provider download. Tracker Player will validate and copy it into the matching tracker row."
            panel.nameFieldStringValue = cleanName.isEmpty ? "download" : cleanName
            panel.canCreateDirectories = true
            guard panel.runModal() == .OK, let url = panel.url else { completionHandler(nil); return }
            if FileManager.default.fileExists(atPath: url.path) {
                failed("Choose a new file name. Tracker Player will not overwrite an existing file.")
                completionHandler(nil); return
            }
            destinations[ObjectIdentifier(download)] = url
            completionHandler(url)
        }

        func download(_ download: WKDownload, didReceive challenge: URLAuthenticationChallenge,
                      completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
            completionHandler(.performDefaultHandling, nil)
        }

        func downloadDidFinish(_ download: WKDownload) {
            guard let url = destinations.removeValue(forKey: ObjectIdentifier(download)) else {
                failed("The provider download finished without a selected destination."); return
            }
            completed(url)
        }

        func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
            destinations.removeValue(forKey: ObjectIdentifier(download))
            failed("Provider download failed: \(error.localizedDescription)")
        }
    }
}
