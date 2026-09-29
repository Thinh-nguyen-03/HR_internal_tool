/*
 * Survey refresh progress: one line of text and a bar. Runs in the browser once
 * a second while visible, so the bar and timer move without server traffic.
 * Culture Index gives no real progress, so the bar is stage-based: each stage
 * eases towards its ceiling and never goes backwards.
 */
window.dash_clientside = Object.assign({}, window.dash_clientside, {
    refreshProgress: {
        /* "Refresh JazzHR": real progress, since the site runs these checks itself. */
        renderJazzhr: function (_tick, data) {
            var HIDE_MS = 6000;
            var hidden = ["refresh-progress is-hidden", "", "", {width: "0%"}, true];
            if (!data || !data.total) {
                return hidden;
            }
            var state = window.__jazzhrProgress || (window.__jazzhrProgress = {});
            if (data.server_ms && state.serverMs !== data.server_ms) {
                state.serverMs = data.server_ms;
                state.offset = Date.now() - data.server_ms;
            }
            var now = Date.now() - (state.offset || 0);
            var count = data.done + " of " + data.total;
            var width = Math.max(3, 100 * data.done / data.total).toFixed(1) + "%";

            if (!data.finished_ms) {
                return ["refresh-progress refresh-progress--running", "Checking JazzHR", count, {width: width}, false];
            }
            if (now - data.finished_ms > HIDE_MS) {
                return hidden;
            }
            if (data.failed > 0) {
                return ["refresh-progress refresh-progress--failed",
                        "Checked " + data.total + ", " + data.failed + " failed", count, {width: "100%"}, false];
            }
            return ["refresh-progress refresh-progress--done", "JazzHR statuses updated", count, {width: "100%"}, false];
        },

        render: function (_tick, data) {
            var HIDE_DONE_MS = 8000;
            var HIDE_FAILED_MS = 30000;
            var ABANDONED_MS = 15 * 60 * 1000;
            var WORKER_STALE_MS = 11 * 60 * 1000;

            var hidden = ["refresh-progress is-hidden", "", "", {width: "0%"}, true];
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
                var s = total % 60;
                return Math.floor(total / 60) + ":" + (s < 10 ? "0" : "") + s;
            }
            function ease(elapsedMs, from, to, halfLifeMs) {
                return from + (to - from) * (1 - Math.exp(-elapsedMs / halfLifeMs));
            }
            function shortError(error) {
                var e = (error || "").toLowerCase();
                if (e.indexOf("504") >= 0 || e.indexOf("timed out") >= 0 || e.indexOf("timeout") >= 0) {
                    return "Culture Index timed out";
                }
                if (e.indexOf("403") >= 0) {
                    return "Culture Index refused the request";
                }
                if (e.indexOf("already running") >= 0) {
                    return "another refresh is running";
                }
                return "something went wrong";
            }

            var sinceRequest = now - data.requested_ms;
            var variant, title, elapsed = "", width;

            if (data.state === "requested") {
                if (sinceRequest > ABANDONED_MS) {
                    return hidden;
                }
                var workerStale = !data.heartbeat_ms || now - data.heartbeat_ms > WORKER_STALE_MS;
                variant = sinceRequest > 20000 && workerStale ? "warning" : "requested";
                title = variant === "warning" ? "Worker isn't responding" : "Waiting for the worker";
                width = ease(sinceRequest, 3, 14, 4000);
            } else if (data.state === "running") {
                if (sinceRequest > ABANDONED_MS) {
                    return hidden;
                }
                var sinceStart = now - (data.started_ms || data.requested_ms);
                variant = "running";
                title = data.attempt > 1 ? "Culture Index timed out, retrying" : "Fetching surveys";
                elapsed = clock(sinceRequest);
                width = ease(sinceStart, 16, 92, 22000);
            } else if (data.state === "done") {
                if (now - (data.finished_ms || now) > HIDE_DONE_MS) {
                    return hidden;
                }
                var n = data.new_count || 0;
                variant = "done";
                title = n > 0 ? "Updated: " + n + " new survey" + (n === 1 ? "" : "s") : "Updated: no new surveys";
                width = 100;
            } else if (data.state === "failed") {
                if (now - (data.finished_ms || now) > HIDE_FAILED_MS) {
                    return hidden;
                }
                variant = "failed";
                title = "Refresh failed: " + shortError(data.error);
                width = 100;
            } else {
                return hidden;
            }

            return [
                "refresh-progress refresh-progress--" + variant,
                title,
                elapsed,
                {width: width.toFixed(1) + "%"},
                false
            ];
        }
    }
});
