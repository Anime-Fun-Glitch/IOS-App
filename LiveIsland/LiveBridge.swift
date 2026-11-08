import Foundation
import ActivityKit

/// Міст Python → Live Activity. Python викликає через rubicon: AnimeLive.setPort(8765), AnimeLive.apply(jsonString).
@objc(AnimeLive)
public final class AnimeLive: NSObject {
    static var port: Int = 8765
    static var state: AnimeActivityAttributes.ContentState?
    static var kind = ""
    static var resetTask: Task<Void, Never>?
    static var hooked = false

    @objc public class func setPort(_ p: Int) {
        port = p
    }

    @objc public class func apply(_ json: String) -> String {
        guard #available(iOS 17.0, *) else { return "ios<17" }
        installHooks()
        if json.isEmpty {
            endAll()
            return "ended"
        }
        guard let data = json.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return "bad json" }
        var st = AnimeActivityAttributes.ContentState(
            kind: obj["kind"] as? String ?? "download",
            title: obj["title"] as? String ?? "",
            subtitle: obj["subtitle"] as? String ?? "",
            progress: obj["progress"] as? Double ?? 0,
            count: obj["count"] as? Int ?? 0,
            paused: obj["paused"] as? Bool ?? false,
            confirm: false,
            until: nil)
        if let u = obj["until"] as? Double, u > 0 { st.until = Date(timeIntervalSince1970: u) }
        if let cur = state, cur.kind == st.kind { st.confirm = cur.confirm }
        state = st
        let existing = Activity<AnimeActivityAttributes>.activities
        if let a = existing.first, kind == st.kind {
            Task { await a.update(ActivityContent(state: st, staleDate: nil)) }
            return "updated"
        }
        if !existing.isEmpty { endAll() }
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return "disabled" }
        do {
            _ = try Activity<AnimeActivityAttributes>.request(attributes: AnimeActivityAttributes(name: "anime"),
                                     content: ActivityContent(state: st, staleDate: nil),
                                     pushType: nil)
            kind = st.kind
            return "started"
        } catch {
            return "error: \(error)"   // напр. застосунок у фоні — Live Activity можна стартувати лише з переднього плану
        }
    }

    @available(iOS 17.0, *)
    static func endAll() {
        kind = ""
        state = nil
        Task {
            for a in Activity<AnimeActivityAttributes>.activities {
                await a.end(nil, dismissalPolicy: .immediate)
            }
        }
    }

    @available(iOS 17.0, *)
    static func push(_ st: AnimeActivityAttributes.ContentState) {
        state = st
        Task {
            for a in Activity<AnimeActivityAttributes>.activities {
                await a.update(ActivityContent(state: st, staleDate: nil))
            }
        }
    }

    @available(iOS 17.0, *)
    static func installHooks() {
        if hooked { return }
        hooked = true
        LiveHooks.onPause = {
            let paused = AnimeLive.state?.paused ?? false
            await AnimeLive.post("/api/dl_pause", ["on": !paused])
            if var st = AnimeLive.state { st.paused = !paused; AnimeLive.push(st) }
        }
        LiveHooks.onCancel = {
            guard var st = AnimeLive.state else { return }
            if !st.confirm {
                // перше натискання: просимо підтвердити; без другого — через 5 с скидаємо
                st.confirm = true
                AnimeLive.push(st)
                AnimeLive.resetTask?.cancel()
                AnimeLive.resetTask = Task {
                    try? await Task.sleep(nanoseconds: 5_000_000_000)
                    if Task.isCancelled { return }
                    if var s2 = AnimeLive.state, s2.confirm { s2.confirm = false; AnimeLive.push(s2) }
                }
            } else {
                AnimeLive.resetTask?.cancel()
                st.confirm = false
                AnimeLive.push(st)
                await AnimeLive.post("/api/cancel", [:])
            }
        }
    }

    static func post(_ path: String, _ body: [String: Any]) async {
        guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { return }
        var r = URLRequest(url: url)
        r.httpMethod = "POST"
        r.timeoutInterval = 6
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try? JSONSerialization.data(withJSONObject: body)
        _ = try? await URLSession.shared.data(for: r)
    }
}
