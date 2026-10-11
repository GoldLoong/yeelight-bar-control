import SafariServices

class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    func beginRequest(with context: NSExtensionContext) {
        // 本扩展用 background fetch 直连本机 API，无需原生消息桥
        context.completeRequest(returningItems: nil, completionHandler: nil)
    }
}
