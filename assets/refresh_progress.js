/*
 * Survey refresh progress card. Runs in the browser once a second while the card
 * is visible, so the bar and timer move smoothly without any server traffic.
 * Culture Index gives no real progress, so the bar is stage-based: each stage
 * eases towards its ceiling and never goes backwards.
 */
window.dash_clientside = Object.assign({}, window.dash_clientside, {
    refreshProgress: {
        render: function (_tick, data) {
            var HIDE_DONE_MS = 12000;
            var HIDE_FAILED_MS = 45000;
            var ABANDONED_MS = 15 * 60 * 1000;
            var WORKER_STALE_MS = 11 * 60 * 1000;
            var STEP = "refresh-progress__step";

            var hidden = ["refresh-progress is-hidden", "", "", {width: "0%"}, STEP, STEP, STEP, "", true];
            if (!data || !data.state || !data.requested_ms) {
                return hidden;
            }

            // Correct for the browser clock: server_ms is the server's time when the data was sent.
            var state = window.__refreshProgress || (window.__refreshProgress = {});
            if (data.server_ms && state.serverMs !== data.server_ms) {
                state.serverMs = data.server_ms;
                state.offset = Date.now() - data.server_ms;
            }
            var now = Date.now() - (state.offset || 0);

            function clock(ms) {
                var total = Math.max(0, Math.floor(ms / 1000));
                var m = Math.floor(total / 60);
                var s = total % 60;
                return m + ":" + (s < 10 ? "0" : "") + s;
            }
            function timeOfDay(ms) {
                return new Date(ms).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
            }
            function ease(elapsedMs, from, to, halfLifeMs) {
                return from + (to - from) * (1 - Math.exp(-elapsedMs / halfLifeMs));
            }

            var sinceRequest = now - data.requested_ms;
            var title, detail, width, elapsed, steps, variant;

            if (data.state === "requested") {
                if (sinceRequest > ABANDONED_MS) {
                    return hidden;
                }
                variant = "requested";
                width = ease(sinceRequest, 3, 14, 4000);
                elapsed = clock(sinceRequest);
                steps = ["is-active", "", ""];
                var workerStale = !data.heartbeat_ms || now - data.heartbeat_ms > WORKER_STALE_MS;
                if (sinceRequest > 20000 && workerStale) {
                    variant = "warning";
                    title = "The worker isn't responding";
                    detail = data.heartbeat_ms
                        ? "Last seen at " + timeOfDay(data.heartbeat_ms) + ". The refresh runs as soon as it's back."
                        : "No word from the worker yet. The refresh runs as soon as it's back.";
                } else {
                    title = "Waiting for the worker";
                    detail = sinceRequest > 15000 ? "Still waiting for the worker to pick this up." : "Starting in a moment.";
                }
            } else if (data.state === "running") {
                if (sinceRequest > ABANDONED_MS) {
                    return hidden;
                }
                variant = "running";
                var sinceStart = now - (data.started_ms || data.requested_ms);
                width = ease(sinceStart, 16, 92, 22000);
                elapsed = clock(sinceRequest);
                steps = ["is-done", "is-active", ""];
                title = "Fetching surveys from Culture Index";
                if (data.attempt > 1) {
                    detail = "Culture Index timed out. Trying again.";
                } else if (sinceStart > 60000) {
                    detail = "Culture Index is slow right now. This can take up to 3 minutes.";
                } else {
                    detail = "Usually takes 15 to 45 seconds.";
                }
            } else if (data.state === "done") {
                var sinceDone = now - (data.finished_ms || now);
                if (sinceDone > HIDE_DONE_MS) {
                    return hidden;
                }
                variant = "done";
                width = 100;
                elapsed = "Took " + clock((data.finished_ms || now) - data.requested_ms);
                steps = ["is-done", "is-done", "is-done"];
                title = "Survey list updated";
                var n = data.new_count || 0;
                detail = n > 0
                    ? n + " new survey" + (n === 1 ? "" : "s") + ". Click New Surveys to see them."
                    : "No new surveys since the last update.";
            } else if (data.state === "failed") {
                var sinceFail = now - (data.finished_ms || now);
                if (sinceFail > HIDE_FAILED_MS) {
                    return hidden;
                }
                variant = "failed";
                width = 100;
                elapsed = "";
                steps = ["is-done", "is-failed", ""];
                title = "Refresh failed";
                detail = (data.error || "Something went wrong.") + " Try again in a minute.";
            } else {
                return hidden;
            }

            return [
                "refresh-progress refresh-progress--" + variant,
                title,
                elapsed,
                {width: width.toFixed(1) + "%"},
                STEP + (steps[0] ? " " + steps[0] : ""),
                STEP + (steps[1] ? " " + steps[1] : ""),
                STEP + (steps[2] ? " " + steps[2] : ""),
                detail,
                false
            ];
        }
    }
});
