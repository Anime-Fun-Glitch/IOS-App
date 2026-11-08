import Foundation
import ActivityKit
import AppIntents

// Спільний файл: компілюється і в застосунок, і в розширення віджета.

@available(iOS 16.2, *)
struct AnimeActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var kind: String        // "download" | "check"
        var title: String
        var subtitle: String
        var progress: Double    // 0...1
        var count: Int          // скільки серій у черзі
        var paused: Bool
        var confirm: Bool       // перше натискання «Скасувати» — чекаємо підтвердження
        var until: Date?        // для таймера перевірки
    }
    var name: String
}

/// Дії кнопок. Виконуються в процесі застосунку (LiveActivityIntent), реальні обробники ставить AnimeLive.
enum LiveHooks {
    static var onPause: (() async -> Void)?
    static var onCancel: (() async -> Void)?
}

@available(iOS 17.0, *)
struct PauseIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Пауза"
    init() {}
    func perform() async throws -> some IntentResult {
        await LiveHooks.onPause?()
        return .result()
    }
}

@available(iOS 17.0, *)
struct CancelIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Скасувати"
    init() {}
    func perform() async throws -> some IntentResult {
        await LiveHooks.onCancel?()
        return .result()
    }
}
