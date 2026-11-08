import WidgetKit
import SwiftUI
import ActivityKit
import AppIntents

@main
struct AnimeWidgets: WidgetBundle {
    var body: some Widget {
        AnimeLiveActivity()
    }
}

private func timerText(_ until: Date?) -> Text {
    if let u = until, u > Date() {
        return Text(timerInterval: Date()...u, countsDown: true)
    }
    return Text("0:00")
}

struct ControlsView: View {
    let s: AnimeActivityAttributes.ContentState
    var body: some View {
        HStack(spacing: 10) {
            Button(intent: PauseIntent()) {
                Label(s.paused ? "Продовжити" : "Пауза", systemImage: s.paused ? "play.fill" : "pause.fill")
                    .font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity)
            }
            .tint(.gray)
            Button(intent: CancelIntent()) {
                Label(s.confirm ? "Точно скасувати?" : "Скасувати", systemImage: s.confirm ? "exclamationmark.triangle.fill" : "xmark")
                    .font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity)
            }
            .tint(s.confirm ? .red : .gray)
        }
        .buttonStyle(.borderedProminent)
    }
}

struct LockView: View {
    let s: AnimeActivityAttributes.ContentState
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if s.kind == "check" {
                HStack {
                    Image(systemName: "clock.fill")
                    Text("Наступна перевірка нових серій").font(.subheadline)
                    Spacer()
                    timerText(s.until).monospacedDigit().font(.title3.weight(.semibold)).frame(maxWidth: 90, alignment: .trailing)
                }
            } else {
                HStack {
                    Image(systemName: s.paused ? "pause.circle.fill" : "arrow.down.circle.fill")
                    VStack(alignment: .leading, spacing: 0) {
                        Text(s.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                        Text(s.subtitle + (s.count > 1 ? " · ще \(s.count - 1) у черзі" : "")).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text("\(Int(s.progress * 100))%").monospacedDigit().font(.title3.weight(.semibold))
                }
                ProgressView(value: s.progress).tint(s.paused ? .gray : .green)
                ControlsView(s: s)
            }
        }
        .padding(14)
    }
}

struct AnimeLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: AnimeActivityAttributes.self) { ctx in
            LockView(s: ctx.state)
                .activityBackgroundTint(Color.black.opacity(0.75))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { ctx in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Image(systemName: ctx.state.kind == "check" ? "clock.fill" : (ctx.state.paused ? "pause.circle.fill" : "arrow.down.circle.fill"))
                        .font(.title2)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    if ctx.state.kind == "check" {
                        timerText(ctx.state.until).monospacedDigit().font(.title3.weight(.semibold)).frame(maxWidth: 80, alignment: .trailing)
                    } else {
                        Text("\(Int(ctx.state.progress * 100))%").monospacedDigit().font(.title3.weight(.semibold))
                    }
                }
                DynamicIslandExpandedRegion(.center) {
                    if ctx.state.kind == "check" {
                        Text("Перевірка нових серій").font(.subheadline)
                    } else {
                        VStack(spacing: 0) {
                            Text(ctx.state.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                            Text(ctx.state.subtitle + (ctx.state.count > 1 ? " · ще \(ctx.state.count - 1)" : "")).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    if ctx.state.kind == "download" {
                        VStack(spacing: 8) {
                            ProgressView(value: ctx.state.progress).tint(ctx.state.paused ? .gray : .green)
                            ControlsView(s: ctx.state)
                        }
                    }
                }
            } compactLeading: {
                Image(systemName: ctx.state.kind == "check" ? "clock.fill" : (ctx.state.paused ? "pause.fill" : "arrow.down"))
            } compactTrailing: {
                if ctx.state.kind == "check" {
                    timerText(ctx.state.until).monospacedDigit().frame(maxWidth: 44)
                } else {
                    Text("\(Int(ctx.state.progress * 100))%").monospacedDigit()
                }
            } minimal: {
                Image(systemName: ctx.state.kind == "check" ? "clock.fill" : "arrow.down")
            }
        }
    }
}
