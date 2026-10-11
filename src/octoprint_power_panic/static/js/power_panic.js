$(function () {
    function PowerPanicViewModel(parameters) {
        var self = this;
        self.loginState = parameters[0];

        self.state = ko.observable("idle");
        self.recovery = ko.observable(null);
        self.busy = ko.observable(false);

        self.visible = ko.pureComputed(function () {
            return self.state() !== "idle";
        });
        self.canResume = ko.pureComputed(function () {
            return self.state() === "pending" && !self.busy();
        });
        self.canDiscard = ko.pureComputed(function () {
            return ["pending", "failed", "detecting", "probing", "reading", "waiting_for_printer"]
                .indexOf(self.state()) >= 0 && !self.busy();
        });
        self.headline = ko.pureComputed(function () {
            switch (self.state()) {
                case "waiting_for_printer":
                    return gettext("Interrupted print found. Waiting for the printer to connect.");
                case "detecting":
                case "probing":
                case "reading":
                    return gettext("Checking the printer for a recovery...");
                case "pending":
                    return gettext("An interrupted print can be resumed.");
                case "resuming":
                    return gettext("Resuming: the printer is reheating and moving back.");
                case "starting":
                    return gettext("Starting the resumed print...");
                case "failed":
                    return gettext("The interrupted print cannot be resumed.");
            }
            return "";
        });
        self.path = ko.pureComputed(function () {
            var r = self.recovery();
            return r ? r.path : "";
        });
        self.progress = ko.pureComputed(function () {
            var r = self.recovery();
            if (!r || !r.size || r.pos === undefined || r.pos === null) return "";
            return Math.round((100 * r.pos) / r.size) + "%";
        });
        self.outage = ko.pureComputed(function () {
            var r = self.recovery();
            if (!r || r.outage === undefined || r.outage === null) return "";
            return Math.round(r.outage / 60) + " min";
        });
        self.mode = ko.pureComputed(function () {
            var r = self.recovery();
            if (!r || !r.mode) return "";
            return r.mode === "firmware"
                ? gettext("Prusa firmware recovery")
                : gettext("Generic recovery (untested on real non-Prusa printers)");
        });
        self.reasons = ko.pureComputed(function () {
            var r = self.recovery();
            return (r && r.reasons) || [];
        });
        self.error = ko.pureComputed(function () {
            var r = self.recovery();
            return (r && r.error) || "";
        });

        self.update = function (status) {
            if (!status) return;
            self.state(status.state);
            self.recovery(status.recovery);
        };

        self.refresh = function () {
            if (!self.loginState.isUser()) return;
            OctoPrint.simpleApiGet("power_panic").done(self.update);
        };
        self.onStartupComplete = self.refresh;
        self.onUserLoggedIn = self.refresh;

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "power_panic") return;
            self.update(data.status);
            var text = {
                pending: gettext("An interrupted print can be resumed. See the Power Panic panel."),
                failed: data.payload && data.payload.error,
                blocked: data.payload && data.payload.reason,
                not_recoverable: data.payload && data.payload.reason,
                resumed: gettext("The interrupted print has resumed."),
            }[data.type];
            if (text) {
                new PNotify({
                    title: gettext("Power Panic"),
                    text: text,
                    type: data.type === "resumed" ? "success" : data.type === "pending" ? "info" : "error",
                    hide: data.type === "resumed",
                });
            }
        };

        self._command = function (command) {
            self.busy(true);
            OctoPrint.simpleApiCommand("power_panic", command)
                .done(self.update)
                .fail(self.refresh)
                .always(function () {
                    self.busy(false);
                });
        };

        self.resume = function () {
            self._command("resume");
        };

        self.discard = function () {
            var message = gettext("The interrupted print will be forgotten.");
            var r = self.recovery();
            if (r && r.mode === "firmware") {
                message += " " + gettext(
                    "The printer will also stop its own recovery, the same as Stop print on its screen."
                );
            }
            showConfirmationDialog({
                message: message,
                proceed: gettext("Discard"),
                onproceed: function () {
                    self._command("discard");
                },
            });
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: PowerPanicViewModel,
        dependencies: ["loginStateViewModel"],
        elements: ["#sidebar_plugin_power_panic_wrapper"],
    });
});
