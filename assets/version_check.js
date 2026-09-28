/*
 * Reloads the page after a deploy. A page loaded before a deploy keeps calling
 * the old Dash callbacks, which the new server no longer has, so it stops
 * updating. Once a minute this compares /version with the build the page was
 * loaded with; a tab in the background reloads when it is next shown.
 */
(function () {
    var CHECK_MS = 60000;
    var loadedBuild = null;
    var pendingReload = false;

    function check() {
        fetch("/version", {cache: "no-store", credentials: "same-origin"})
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) {
                if (!data || !data.build) {
                    return;
                }
                if (loadedBuild === null) {
                    loadedBuild = data.build;
                } else if (data.build !== loadedBuild) {
                    pendingReload = true;
                    reloadIfVisible();
                }
            })
            .catch(function () { /* deploy in progress or offline; try again next time */ });
    }

    function reloadIfVisible() {
        if (pendingReload && document.visibilityState === "visible") {
            window.location.reload();
        }
    }

    document.addEventListener("visibilitychange", reloadIfVisible);
    check();
    setInterval(check, CHECK_MS);
})();
