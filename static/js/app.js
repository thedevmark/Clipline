/**
 * Shared shell for Clipline: dependency setup, onboarding, settings panels,
 * keyboard shortcuts. The reel video editor lives in reel.js.
 */
const App = (() => {
    const AUTO_DOWNLOAD_CONSENT_KEY = "alertCreatorAutoDownloadConsent";
    const SETTINGS_STORAGE_KEY = "cliplineSettings";
    const DEFAULT_THEME_ID = "cobalt-night";
    const AVAILABLE_THEME_IDS = new Set([
        "cobalt-night",
        "graphite-terminal",
        "nord-stack",
        "forest-syntax",
        "signal-teal",
    ]);

    let dependencyInstallAttempted = false;
    let dependencyInstallInFlight = false;
    let dependencyUpdateInFlight = false;
    let captioningInstallInFlight = false;
    let lastDeps = null;
    let onboardingFlow = null;
    let onboardingStep = 0;
    let dependencyDropdownCloseHandlersBound = false;
    let dependencyBannerTimer = null;
    let storageConfig = null;

    const ONBOARDING_FLOWS = {
        reel: [
            {
                title: "Video Editor Is The Twitch VOD Workflow",
                body: "Use it when a stream session needs to turn into many deliverables: imported clips, polished shorts, captions, and a longform follow-up.",
                features: [
                    "Connect Twitch and pull recent VODs, markers, and viewer clips",
                    "Build a session inbox from one stream instead of one isolated file",
                    "Prep shorts, stitch clip sequences, and keep the project autosaved locally",
                    "Queue the strongest shorts into a later longform cut",
                ],
            },
            {
                title: "Install required tools",
                body: "Install the core download and processing tools once. After that, the workflow can pull sources, clips, and exports locally.",
                isRuntimeStep: true,
            },
            {
                title: "Set Up Auto Captions",
                body: "Captions need two Python packages in the same environment as the app. Install them here now or leave this for later.",
                isDepsStep: true,
            },
            {
                title: "Ingest The Stream Session",
                body: "Load a VOD, local recording, or connected Twitch archive. Then pull markers and existing clips into the inbox.",
                features: [
                    "Load a Twitch VOD directly from the connected account",
                    "Import Twitch markers and viewer clips as starter edits",
                    "Paste stream notes or marker timestamps to seed the inbox",
                ],
            },
            {
                title: "Build The Inbox, Then Prep Shorts",
                body: "Refine clips, preview the sequence, and turn the best moments into short-ready pieces.",
                features: [
                    "Review imported moments alongside manual clips",
                    "Prep one active clip or bulk prep the whole inbox",
                    "Keep shortform choices and the longform queue in the same project",
                ],
            },
            {
                title: "Polish Captions And Publish Outputs",
                body: "Run captions, choose delivery format, render shorts, and spin up the longform project when the session is ready.",
                features: [
                    "Burn captions into shorts or keep them for later",
                    "Export Shorts, square, 4:5, or landscape versions",
                    "Build a separate longform project from queued prepared shorts",
                ],
                isLast: true,
                doneLabel: "Open Video Editor",
            },
        ],
    };

    // ── Helpers ─────────────────────────────────────────────────

    function $(id) {
        return document.getElementById(id);
    }

    function show(el) {
        if (typeof el === "string") el = $(el);
        if (el) el.classList.remove("hidden");
    }

    function hide(el) {
        if (typeof el === "string") el = $(el);
        if (el) el.classList.add("hidden");
    }

    function escapeHtml(value) {
        return String(value || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function normalizeThemeId(themeId) {
        const normalized = String(themeId || "").trim();
        return AVAILABLE_THEME_IDS.has(normalized) ? normalized : DEFAULT_THEME_ID;
    }

    function applyTheme(themeId) {
        const normalized = normalizeThemeId(themeId);
        document.documentElement.dataset.theme = normalized;
        const select = $("setting-theme");
        if (select && select.value !== normalized) {
            select.value = normalized;
        }
        return normalized;
    }

    async function api(endpoint, options = {}) {
        const resp = await fetch(endpoint, {
            headers: { "Content-Type": "application/json" },
            ...options,
        });
        return resp.json();
    }

    // ── Auto-download consent ───────────────────────────────────

    function getAutoDownloadConsent() {
        try {
            return localStorage.getItem(AUTO_DOWNLOAD_CONSENT_KEY) || "";
        } catch (e) {
            return "";
        }
    }

    function setAutoDownloadConsent(value) {
        try {
            localStorage.setItem(AUTO_DOWNLOAD_CONSENT_KEY, value);
        } catch (e) {
            // Ignore storage errors.
        }
    }

    function buildDependencyDisclosureHtml(deps) {
        const disclosure = deps?.download_disclosure || {};
        const runtimePath = escapeHtml(disclosure.runtime_path || "your local app runtime folder");
        const sources = disclosure.sources || {};
        const ffmpegSource = escapeHtml(sources.ffmpeg || "FFmpeg mirror");
        const ytdlpSource = escapeHtml(sources["yt-dlp"] || "yt-dlp releases");
        const denoSource = escapeHtml(sources.deno || "Deno releases");
        return [
            "<strong>Before download:</strong> this app can save runtime tools on your computer.",
            `<strong>Install location:</strong> <code>${runtimePath}</code>`,
            "<strong>Tools:</strong> required ffmpeg/ffprobe + yt-dlp, optional deno",
            `<strong>Sources:</strong><br><code>${ffmpegSource}</code><br><code>${ytdlpSource}</code><br><code>${denoSource}</code>`,
        ].join("<br>");
    }

    function showAutoDownloadConsentPrompt(deps) {
        setPanelOpen("dependency-settings-panel", true);
        const actions = `
            <div class="dep-choice-actions">
                <button type="button" class="dep-choice-btn" onclick="App.allowDependencyAutoDownload()">Allow Auto-Download</button>
                <button type="button" class="dep-choice-btn secondary-btn" onclick="App.useManualDependencySetup()">Manual Install Only</button>
            </div>
        `;
        setDependencyBanner(
            "<strong>Permission required:</strong> allow dependency downloads?",
            `${buildDependencyDisclosureHtml(deps)}${actions}`,
            false
        );
    }

    async function allowDependencyAutoDownload() {
        setAutoDownloadConsent("allow");
        dependencyInstallAttempted = true;
        await installMissingDependencies(false);
    }

    function useManualDependencySetup() {
        setAutoDownloadConsent("manual");
        setDependencyBanner(
            "<strong>Auto-download is disabled.</strong>",
            "Install tools manually from the Dependency Setup links, or click <strong>Auto Install Missing</strong> any time to opt in later.",
            false
        );
    }

    // ── Storage config ──────────────────────────────────────────

    function renderStorageConfig(config) {
        storageConfig = config || null;
        const input = $("output-dir-input");
        const status = $("output-dir-status");
        if (!input || !status || !storageConfig) return;
        input.value = storageConfig.output_dir || "";
        const usingDefault = !storageConfig.custom_output_dir;
        status.textContent = usingDefault
            ? `Finished exports save to the default folder: ${storageConfig.output_dir}`
            : `Finished exports save to: ${storageConfig.output_dir}`;
    }

    async function loadStorageConfig() {
        try {
            const config = await api("/api/storage-config");
            renderStorageConfig(config);
        } catch (e) {
            const status = $("output-dir-status");
            if (status) status.textContent = "Could not load save location settings.";
        }
    }

    async function applyOutputFolder() {
        const input = $("output-dir-input");
        if (!input) return;
        const data = await api("/api/storage-config", {
            method: "PUT",
            body: JSON.stringify({ output_dir: input.value.trim() }),
        });
        if (data.error) {
            setDependencyBanner(
                "<strong>Save location update failed.</strong>",
                escapeHtml(data.error),
                true
            );
            return;
        }
        renderStorageConfig(data);
    }

    async function resetOutputFolder() {
        const data = await api("/api/storage-config/reset", { method: "POST" });
        if (data.error) {
            setDependencyBanner(
                "<strong>Save location reset failed.</strong>",
                escapeHtml(data.error),
                true
            );
            return;
        }
        renderStorageConfig(data);
    }

    async function chooseOutputFolder() {
        const data = await api("/api/storage-config/choose", { method: "POST" });
        if (data.status === "cancelled") return;
        if (data.error) {
            setDependencyBanner(
                "<strong>Folder picker failed.</strong>",
                `${escapeHtml(data.error)}<br>Paste a path into Save Location and click <strong>Use Path</strong> instead.`,
                true
            );
            return;
        }
        renderStorageConfig(data);
    }

    // ── Dependency banner & panels ──────────────────────────────

    function setDependencyBanner(messageHtml, instructionsHtml = "", showAsError = true, autoHideMs = 0) {
        const banner = $("dep-banner");
        const msg = $("dep-message");
        const instEl = $("dep-instructions");
        if (!banner || !msg || !instEl) return;
        if (dependencyBannerTimer) {
            clearTimeout(dependencyBannerTimer);
            dependencyBannerTimer = null;
        }
        msg.innerHTML = messageHtml;
        instEl.innerHTML = instructionsHtml;
        banner.classList.toggle("banner-error", showAsError);
        show(banner);
        if (autoHideMs > 0) {
            dependencyBannerTimer = setTimeout(() => {
                hideDependencyBanner();
            }, autoHideMs);
        }
    }

    function hideDependencyBanner() {
        if (dependencyBannerTimer) {
            clearTimeout(dependencyBannerTimer);
            dependencyBannerTimer = null;
        }
        hide("dep-banner");
    }

    function setPanelOpen(panelId, isOpen) {
        const panel = $(panelId);
        if (!panel) return;
        panel.classList.toggle("open", !!isOpen);
        document.querySelectorAll(`[data-panel="${panelId}"]`).forEach((trigger) => {
            trigger.classList.toggle("active", !!isOpen);
        });
    }

    function toggleSettingsPanel(panelId) {
        const panel = $(panelId);
        if (!panel) return;
        const shouldOpen = !panel.classList.contains("open");
        setPanelOpen(panelId, shouldOpen);
    }

    function openSettingsPanel(panelId, stepId = "") {
        const panel = $(panelId);
        if (!panel) return;
        setPanelOpen(panelId, true);

        const step = stepId ? $(stepId) : panel.closest(".step");
        if (step) {
            step.classList.remove("disabled");
            step.scrollIntoView({ behavior: "smooth", block: "start" });
        } else {
            panel.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    }

    function bindDependencyDropdownCloseHandlers() {
        if (dependencyDropdownCloseHandlersBound) return;
        dependencyDropdownCloseHandlersBound = true;

        document.addEventListener("click", (event) => {
            const panel = $("dependency-settings-panel");
            const toggleBtn = $("dependency-settings-toggle");
            if (!panel || !toggleBtn || !panel.classList.contains("open")) return;

            const target = event.target;
            if (panel.contains(target) || toggleBtn.contains(target)) return;
            setPanelOpen("dependency-settings-panel", false);
        });

        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            const panel = $("dependency-settings-panel");
            if (!panel || !panel.classList.contains("open")) return;
            setPanelOpen("dependency-settings-panel", false);
        });
    }

    // ── Dependency status rendering ─────────────────────────────

    function renderDependencyStatus(deps) {
        lastDeps = deps;
        document.dispatchEvent(new CustomEvent("dm:deps-status", {
            detail: deps,
        }));
        const missing = [];
        const instructions = [];
        const captioning = deps.captioning || {};
        const captionInstallState = deps.captioning_install || {};
        const captionRequiredMissing = captioning.required_missing || [];
        const captionOptionalMissing = captioning.optional_missing || [];

        const ffmpegStatus = $("dep-ffmpeg-status");
        const ytdlpStatus = $("dep-ytdlp-status");
        const denoStatus = $("dep-deno-status");
        const fasterWhisperStatus = $("dep-faster-whisper-status");
        const torchStatus = $("dep-torch-status");
        const pyannoteStatus = $("dep-pyannote-status");
        const installBtn = $("auto-install-deps-btn");
        const updateBtn = $("update-ytdlp-btn");

        if (deps.ffmpeg?.installed && deps.ffprobe?.installed) {
            ffmpegStatus.textContent = "✓ Installed";
            ffmpegStatus.className = "dep-status installed";
        } else {
            ffmpegStatus.textContent = "✗ Missing";
            ffmpegStatus.className = "dep-status missing";
            missing.push("FFmpeg/ffprobe");
            instructions.push("FFmpeg: Use Auto Install in Dependency Setup (top-right). If needed, run 'winget install Gyan.FFmpeg'.");
        }

        if (deps["yt-dlp"]?.installed) {
            ytdlpStatus.textContent = "✓ Installed";
            ytdlpStatus.className = "dep-status installed";
        } else {
            ytdlpStatus.textContent = "✗ Missing";
            ytdlpStatus.className = "dep-status missing";
            missing.push("yt-dlp");
            instructions.push("yt-dlp: Use Auto Install in Dependency Setup (top-right), or install manually.");
        }

        if (denoStatus) {
            if (deps.deno?.installed) {
                denoStatus.textContent = "✓ Installed";
                denoStatus.className = "dep-status installed";
            } else {
                denoStatus.textContent = "⚠ Optional (Missing)";
                denoStatus.className = "dep-status missing";
                instructions.push("When auto-download is allowed, Deno install is attempted by default. If still missing, retry Auto Install or run 'winget install DenoLand.Deno' for better YouTube challenge handling.");
            }
        }

        if (fasterWhisperStatus) {
            if (captioning.faster_whisper?.installed) {
                const version = captioning.faster_whisper.version || "installed";
                fasterWhisperStatus.textContent = `✓ ${version}`;
                fasterWhisperStatus.className = "dep-status installed";
            } else {
                fasterWhisperStatus.textContent = "✗ Needed";
                fasterWhisperStatus.className = "dep-status missing";
            }
        }

        if (torchStatus) {
            if (captioning.torch?.installed) {
                const device = captioning.torch.cuda ? "CUDA" : "CPU";
                torchStatus.textContent = `✓ ${device}`;
                torchStatus.className = "dep-status installed";
            } else {
                torchStatus.textContent = "✗ Needed";
                torchStatus.className = "dep-status missing";
            }
        }

        if (pyannoteStatus) {
            if (captioning.pyannote_audio?.installed) {
                const version = captioning.pyannote_audio.version || "installed";
                pyannoteStatus.textContent = `✓ ${version}`;
                pyannoteStatus.className = "dep-status installed";
            } else {
                pyannoteStatus.textContent = "⚠ Optional";
                pyannoteStatus.className = "dep-status missing";
            }
        }

        if (captionRequiredMissing.length > 0) {
            instructions.push("Captions: Use the 1-click install above. It installs `faster-whisper` and `torch` into this app's managed captioning environment.");
        }
        if (captionOptionalMissing.includes("pyannote_audio")) {
            instructions.push("Speaker labels: Install `pyannote.audio` with the optional 1-click button, then provide a Hugging Face token in Video Editor.");
        }

        if (installBtn) {
            const shouldShowInstall = deps.auto_install_available && (missing.length > 0 || !deps.deno?.installed);
            installBtn.classList.toggle("hidden", !shouldShowInstall);
            installBtn.disabled = dependencyInstallInFlight || dependencyUpdateInFlight || deps.ytdlp_update?.status === "updating";
        }

        if (updateBtn) {
            const updateAvailable = !!deps.ytdlp_update_available;
            const updateStatus = deps.ytdlp_update?.status || "idle";
            updateBtn.classList.toggle("hidden", !updateAvailable);
            updateBtn.disabled = dependencyUpdateInFlight
                || updateStatus === "updating"
                || dependencyInstallInFlight
                || deps.bootstrap?.status === "installing";
            updateBtn.textContent = updateStatus === "updating"
                ? "Updating yt-dlp..."
                : "Update yt-dlp (1-click)";
        }

        const captioningInstallBtn = $("install-captioning-deps-btn");
        if (captioningInstallBtn) {
            const busy = captioningInstallInFlight || captionInstallState.status === "installing";
            captioningInstallBtn.classList.toggle("hidden", captionRequiredMissing.length === 0);
            captioningInstallBtn.disabled = busy;
            if (!busy) captioningInstallBtn.textContent = "Install faster-whisper + torch (1-click)";
        }

        const pyannoteInstallBtn = $("install-pyannote-btn");
        if (pyannoteInstallBtn) {
            const busy = captioningInstallInFlight || captionInstallState.status === "installing";
            const showPyannote = captionOptionalMissing.includes("pyannote_audio") && captionRequiredMissing.length === 0;
            pyannoteInstallBtn.classList.toggle("hidden", !showPyannote);
            pyannoteInstallBtn.disabled = busy;
            if (!busy) pyannoteInstallBtn.textContent = "Install pyannote.audio (1-click)";
        }

        const depPillValue = $("dependency-pill-value");
        if (depPillValue) {
            if (deps.bootstrap?.status === "installing") {
                depPillValue.textContent = "Installing...";
            } else if (deps.ytdlp_update?.status === "updating") {
                depPillValue.textContent = "Updating yt-dlp...";
            } else if (captionInstallState.status === "installing") {
                depPillValue.textContent = "Installing captions...";
            } else if (missing.length === 0) {
                depPillValue.textContent = captionRequiredMissing.length > 0 ? "Captions Need Setup" : "Ready";
            } else {
                depPillValue.textContent = `Needs Setup (${missing.length})`;
            }
        }

        if (missing.length > 0) {
            setPanelOpen("dependency-settings-panel", true);
            const bootstrapMessage = deps.bootstrap?.message || "";
            const bootstrapError = deps.bootstrap?.last_error || "";
            const extra = bootstrapError
                ? `<br><strong>Auto-install error:</strong> ${bootstrapError}`
                : (bootstrapMessage ? `<br>${bootstrapMessage}` : "");
            setDependencyBanner(
                `<strong>Missing dependencies:</strong> ${missing.join(", ")}${extra}`,
                `<strong>How to fix:</strong><br>${instructions.join("<br>")}`,
                true
            );
        } else if (captionRequiredMissing.length > 0) {
            setPanelOpen("dependency-settings-panel", true);
            if (captionInstallState.status === "installing") {
                setDependencyBanner(
                    "<strong>Installing captioning packages...</strong>",
                    escapeHtml(captionInstallState.message || "This may take a few minutes. Do not close the app."),
                    false
                );
            } else if (captionInstallState.status === "failed") {
                setDependencyBanner(
                    "<strong>Captioning install failed.</strong>",
                    escapeHtml(captionInstallState.last_error || "Retry the 1-click install and check internet access."),
                    true
                );
            } else {
                setDependencyBanner(
                    "<strong>Video Editor captioning needs Python packages.</strong>",
                    `<strong>How to fix:</strong><br>${instructions.join("<br>")}`,
                    false
                );
            }
        } else {
            hideDependencyBanner();
        }

        return missing;
    }

    // ── Dependency installs ─────────────────────────────────────

    async function installMissingDependencies(manual = false) {
        const consent = getAutoDownloadConsent();
        if (consent !== "allow") {
            if (!manual) {
                return;
            }
            setAutoDownloadConsent("allow");
        }

        if (dependencyInstallInFlight) return;
        dependencyInstallInFlight = true;
        const installBtn = $("auto-install-deps-btn");
        const previousLabel = installBtn?.textContent || "";
        if (installBtn) {
            installBtn.disabled = true;
            installBtn.textContent = "Installing...";
        }
        setDependencyBanner(
            "<strong>Installing dependencies...</strong> This may take a minute on first run.",
            "Downloading missing tools to your local app folder (includes optional Deno).",
            false
        );
        try {
            const deps = await api("/api/bootstrap-deps", { method: "POST" });
            renderDependencyStatus(deps);
            const requiredMissingCount = (deps.required_missing || []).length;
            const denoMissing = !deps.deno?.installed;
            if (requiredMissingCount === 0 && !denoMissing) {
                if (installBtn) installBtn.classList.add("hidden");
            } else if (manual && requiredMissingCount > 0) {
                setDependencyBanner(
                    "<strong>Dependencies are still missing.</strong>",
                    "Use the dependency troubleshooting list, then restart the app after manual install.",
                    true
                );
            }
        } catch (e) {
            setDependencyBanner(
                "<strong>Dependency installation failed.</strong>",
                "Check your internet connection and try Auto Install again.",
                true
            );
        } finally {
            dependencyInstallInFlight = false;
            if (installBtn && !installBtn.classList.contains("hidden")) {
                installBtn.disabled = false;
                installBtn.textContent = previousLabel || "Auto Install Missing";
            }
        }
    }

    async function updateYtdlp() {
        if (dependencyUpdateInFlight) return;
        dependencyUpdateInFlight = true;

        const updateBtn = $("update-ytdlp-btn");
        const previousLabel = updateBtn?.textContent || "";
        if (updateBtn) {
            updateBtn.disabled = true;
            updateBtn.textContent = "Updating yt-dlp...";
        }

        setDependencyBanner(
            "<strong>Updating yt-dlp...</strong>",
            "Downloading the latest yt-dlp build to your local app runtime folder.",
            false
        );

        try {
            const deps = await api("/api/update-ytdlp", { method: "POST" });
            renderDependencyStatus(deps);

            const updateState = deps?.ytdlp_update || {};
            const updateError = updateState.last_error || "";
            const updateMessage = updateState.message || "";
            if (updateState.status === "failed") {
                setDependencyBanner(
                    "<strong>yt-dlp update failed.</strong>",
                    updateError || "Check internet access and retry.",
                    true
                );
            } else {
                setDependencyBanner(
                    "<strong>yt-dlp is ready.</strong>",
                    escapeHtml(updateMessage || "Update complete."),
                    false,
                    4000
                );
            }
        } catch (e) {
            setDependencyBanner(
                "<strong>yt-dlp update failed.</strong>",
                "Check your internet connection and try again.",
                true
            );
        } finally {
            dependencyUpdateInFlight = false;
            if (updateBtn && !updateBtn.classList.contains("hidden")) {
                updateBtn.disabled = false;
                updateBtn.textContent = previousLabel || "Update yt-dlp (1-click)";
            }
        }
    }

    async function waitForCaptioningInstall(timeoutMs = 30 * 60 * 1000) {
        const startedAt = Date.now();
        let deps = await api("/api/check-deps");
        while ((deps?.captioning_install?.status || "idle") === "installing") {
            if ((Date.now() - startedAt) > timeoutMs) {
                throw new Error("Captioning install timed out.");
            }
            await new Promise((resolve) => setTimeout(resolve, 2000));
            deps = await api("/api/check-deps");
        }
        return deps;
    }

    function getCaptionInstallButtons(includePyannote = false) {
        const ids = includePyannote
            ? ["install-pyannote-btn", "reel-caption-runtime-speaker-btn"]
            : ["install-captioning-deps-btn", "reel-caption-runtime-install-btn"];
        return ids.map((id) => $(id)).filter(Boolean);
    }

    function getCaptionInstallIdleLabel(buttonId, includePyannote = false) {
        if (includePyannote) {
            return buttonId === "reel-caption-runtime-speaker-btn"
                ? "Install pyannote.audio (optional)"
                : "Install pyannote.audio (1-click)";
        }
        return "Install faster-whisper + torch (1-click)";
    }

    function setCaptionInstallButtonState(includePyannote = false, busy = false) {
        const busyLabel = includePyannote
            ? "Installing pyannote.audio..."
            : "Installing faster-whisper + torch...";
        getCaptionInstallButtons(includePyannote).forEach((button) => {
            button.disabled = busy;
            button.textContent = busy
                ? busyLabel
                : getCaptionInstallIdleLabel(button.id, includePyannote);
        });
    }

    async function installCaptioningDeps(includePyannote = false) {
        if (captioningInstallInFlight) return;
        captioningInstallInFlight = true;
        const label = includePyannote ? "pyannote.audio" : "faster-whisper + torch";
        setCaptionInstallButtonState(includePyannote, true);

        setDependencyBanner(
            `<strong>Installing ${label}...</strong>`,
            "This may take a few minutes. Do not close the app.",
            false
        );

        try {
            let deps = await api("/api/install-captioning-deps", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ include_pyannote: includePyannote }),
            });
            renderDependencyStatus(deps);

            if ((deps?.captioning_install?.status || "idle") === "installing") {
                deps = await waitForCaptioningInstall();
                renderDependencyStatus(deps);
            }

            const installState = deps?.captioning_install || {};
            if (installState.status === "failed") {
                setDependencyBanner(
                    `<strong>${label} install failed.</strong>`,
                    escapeHtml(installState.last_error || "Check internet access and retry."),
                    true
                );
            } else {
                setDependencyBanner(
                    `<strong>${label} installed.</strong>`,
                    escapeHtml(installState.message || "Captioning is ready."),
                    false,
                    8000
                );
            }
        } catch (e) {
            setDependencyBanner(
                `<strong>${label} install failed.</strong>`,
                "Check your internet connection and try again.",
                true
            );
        } finally {
            captioningInstallInFlight = false;
            setCaptionInstallButtonState(includePyannote, false);
        }
    }

    // ── Onboarding ──────────────────────────────────────────────

    function showOnboarding(flow, options = {}) {
        const startStep = Number(options.step || 0);
        onboardingFlow = flow;
        onboardingStep = Math.max(0, Math.min(startStep, (ONBOARDING_FLOWS[flow] || []).length - 1));
        renderOnboardingStep();
        const overlay = $("onboarding-overlay");
        if (overlay) overlay.classList.remove("hidden");
    }

    function skipOnboarding() {
        const overlay = $("onboarding-overlay");
        if (overlay) overlay.classList.add("hidden");
        markOnboardingDone(onboardingFlow);
        onboardingFlow = null;
    }

    function markOnboardingDone(flow) {
        if (flow) localStorage.setItem(`onboarding_${flow}_done`, "1");
    }

    function onboardingNext() {
        const flow = ONBOARDING_FLOWS[onboardingFlow];
        if (!flow) return;
        const step = flow[onboardingStep];
        if (step && step.isLast) {
            markOnboardingDone(onboardingFlow);
            const overlay = $("onboarding-overlay");
            if (overlay) overlay.classList.add("hidden");
            onboardingFlow = null;
            return;
        }
        onboardingStep = Math.min(onboardingStep + 1, flow.length - 1);
        renderOnboardingStep();
    }

    function onboardingBack() {
        if (onboardingStep > 0) {
            onboardingStep--;
            renderOnboardingStep();
        }
    }

    function restartOnboarding() {
        setPanelOpen("dependency-settings-panel", false);
        showOnboarding("reel", { step: 0 });
    }

    function checkOnboardingForMode(mode) {
        if (onboardingFlow) return;
        if (!localStorage.getItem(`onboarding_${mode}_done`)) {
            showOnboarding(mode, { step: 0 });
        }
    }

    function renderOnboardingStep() {
        const flow = ONBOARDING_FLOWS[onboardingFlow];
        if (!flow) return;
        const step = flow[onboardingStep];
        const total = flow.length;
        const current = onboardingStep + 1;

        const progressFill = $("onboarding-progress-fill");
        if (progressFill) progressFill.style.width = `${(current / total) * 100}%`;

        const stepLabel = $("onboarding-step-label");
        if (stepLabel) stepLabel.textContent = `Step ${current} of ${total}`;

        const icon = $("onboarding-icon");
        if (icon) {
            const iconText = String(step.icon || "").trim();
            icon.textContent = iconText;
            icon.classList.toggle("hidden", !iconText);
        }

        const title = $("onboarding-title");
        if (title) title.textContent = step.title;

        const body = $("onboarding-body");
        if (body) body.textContent = step.body;

        const contentArea = $("onboarding-content-area");
        if (contentArea) {
            if (step.isRuntimeStep) {
                contentArea.innerHTML = buildOnboardingRuntimeHtml();
            } else if (step.isDepsStep) {
                contentArea.innerHTML = buildOnboardingDepsHtml();
            } else if (step.features) {
                contentArea.innerHTML = `<div class="onboarding-features">${
                    step.features.map(f => `<div class="onboarding-feature"><div class="onboarding-feature-dot"></div><span>${escapeHtml(f)}</span></div>`).join("")
                }</div>`;
            } else {
                contentArea.innerHTML = "";
            }
        }

        const backBtn = $("onboarding-back-btn");
        if (backBtn) backBtn.classList.toggle("hidden", onboardingStep === 0);

        const nextBtn = $("onboarding-next-btn");
        if (nextBtn) {
            nextBtn.disabled = false;
            nextBtn.textContent = step.isLast ? (step.doneLabel || "Done") : "Next";
        }
    }

    function buildOnboardingRuntimeHtml() {
        const ffmpegOk = lastDeps?.ffmpeg?.installed && lastDeps?.ffprobe?.installed;
        const ytdlpOk  = !!lastDeps?.["yt-dlp"]?.installed;
        const allOk    = ffmpegOk && ytdlpOk;
        const busy     = dependencyInstallInFlight || lastDeps?.bootstrap?.status === "installing";

        const rows = [
            { name: "FFmpeg / ffprobe", installed: ffmpegOk, label: ffmpegOk ? "Installed" : "Missing" },
            { name: "yt-dlp",           installed: ytdlpOk,  label: ytdlpOk  ? "Installed" : "Missing" },
        ];

        const rowsHtml = rows.map(r => `
            <div class="onboarding-dep-row">
                <span class="onboarding-dep-name">${escapeHtml(r.name)}</span>
                <span class="onboarding-dep-status ${r.installed ? "installed" : "missing"}">${escapeHtml(r.label)}</span>
            </div>`).join("");

        const installBtn = !allOk ? `
            <button class="onboarding-install-btn" onclick="App.onboardingInstallRuntime()" ${busy ? "disabled" : ""}>
                ${busy ? "Installing Tools..." : "Install Required Tools"}
            </button>` : "";

        const note = allOk
            ? `<p class="onboarding-skip-note success">All required tools are installed. You can move on.</p>`
            : `<p class="onboarding-skip-note">Downloads into the app folder. No admin setup is required.</p>`;

        return `<div class="onboarding-deps-box">${rowsHtml}</div>${installBtn}${note}`;
    }

    async function onboardingInstallRuntime() {
        dependencyInstallInFlight = true;
        renderOnboardingStep();
        try {
            const deps = await api("/api/bootstrap-deps", { method: "POST" });
            lastDeps = deps;
            renderDependencyStatus(deps);
        } catch (_) {}
        dependencyInstallInFlight = false;
        renderOnboardingStep();
    }

    function buildOnboardingDepsHtml() {
        const captioning = lastDeps?.captioning || {};
        const installState = lastDeps?.captioning_install || {};
        const fwInstalled = !!captioning.faster_whisper?.installed;
        const torchInstalled = !!captioning.torch?.installed;
        const pyannoteInstalled = !!captioning.pyannote_audio?.installed;
        const bothRequired = fwInstalled && torchInstalled;
        const busy = captioningInstallInFlight || installState.status === "installing";

        const rows = [
            {
                name: "faster-whisper",
                installed: fwInstalled,
                label: fwInstalled ? (captioning.faster_whisper?.version || "Installed") : "Missing",
            },
            {
                name: "torch",
                installed: torchInstalled,
                label: torchInstalled ? (captioning.torch?.cuda ? "CUDA ready" : "CPU ready") : "Missing",
            },
            {
                name: "pyannote.audio",
                installed: pyannoteInstalled,
                label: pyannoteInstalled ? (captioning.pyannote_audio?.version || "Installed") : "Optional",
            },
        ];

        const rowsHtml = rows.map(r => `
            <div class="onboarding-dep-row">
                <span class="onboarding-dep-name">${escapeHtml(r.name)}</span>
                <span class="onboarding-dep-status ${r.installed ? "installed" : "missing"}">${escapeHtml(r.label)}</span>
            </div>`).join("");

        const installBtn = !bothRequired ? `
            <button class="onboarding-install-btn" onclick="App.onboardingInstallCaptioning(false)" ${busy ? "disabled" : ""}>
                ${busy ? "Installing Caption Runtime..." : "Install Caption Runtime"}
            </button>` : "";

        const pyannoteBtn = bothRequired && !pyannoteInstalled ? `
            <button class="onboarding-install-btn secondary" onclick="App.onboardingInstallCaptioning(true)" ${busy ? "disabled" : ""}>
                ${busy ? "Installing Speaker Labels..." : "Install Speaker Labels Runtime"}
            </button>` : "";

        let skipNote = bothRequired
            ? `<p class="onboarding-skip-note success">Required caption packages are installed.</p>`
            : `<p class="onboarding-skip-note">You can skip this step and install captioning later.</p>`;

        if (installState.status === "installing") {
            skipNote = `<p class="onboarding-skip-note">${escapeHtml(installState.message || "Installing captioning packages...")}</p>`;
        } else if (installState.status === "failed" && !bothRequired) {
            skipNote = `<p class="onboarding-skip-note">${escapeHtml(installState.last_error || "Captioning install failed. Retry when ready.")}</p>`;
        }

        return `<div class="onboarding-deps-box">${rowsHtml}</div>${installBtn}${pyannoteBtn}${skipNote}`;
    }

    async function onboardingInstallCaptioning(includePyannote) {
        await installCaptioningDeps(includePyannote);
        try {
            const deps = await api("/api/check-deps");
            lastDeps = deps;
        } catch (_) {}
        renderOnboardingStep();
    }

    // ── Keyboard shortcuts ──────────────────────────────────────

    function initKeyboardShortcuts() {
        document.addEventListener("keydown", (e) => {
            const tag = document.activeElement?.tagName?.toLowerCase();
            if (tag === "input" || tag === "textarea" || tag === "select") return;
            if (e.ctrlKey || e.metaKey || e.altKey) return;

            if (e.key === "?" || e.key === "/") {
                e.preventDefault();
                toggleShortcutHelp();
                return;
            }

            const video = $("reel-preview-video");
            if (!video) return;
            if (e.key === " ") {
                e.preventDefault();
                video.paused ? video.play() : video.pause();
            } else if (e.key === "l" || e.key === "L") {
                e.preventDefault();
                video.loop = !video.loop;
            } else if (e.key === "ArrowLeft") {
                e.preventDefault();
                video.currentTime = Math.max(0, video.currentTime - (e.shiftKey ? 5 : 1 / 30));
            } else if (e.key === "ArrowRight") {
                e.preventDefault();
                video.currentTime = Math.min(video.duration || 0, video.currentTime + (e.shiftKey ? 5 : 1 / 30));
            }
        });
    }

    function toggleShortcutHelp() {
        let el = $("shortcut-help-overlay");
        if (el) { el.remove(); return; }
        el = document.createElement("div");
        el.id = "shortcut-help-overlay";
        el.className = "shortcut-help-overlay";
        el.innerHTML = `
            <div class="shortcut-help-card">
                <div class="shortcut-help-header">
                    <h3>Keyboard Shortcuts</h3>
                    <button onclick="this.closest('.shortcut-help-overlay').remove()" class="shortcut-help-close">&times;</button>
                </div>
                <div class="shortcut-help-cols">
                    <div>
                        <p class="shortcut-section">Preview</p>
                        <dl>
                            <dt>Space</dt><dd>Play / Pause</dd>
                            <dt>← →</dt><dd>Step 1 frame</dd>
                            <dt>Shift + ← →</dt><dd>Step 5 seconds</dd>
                            <dt>L</dt><dd>Toggle Loop</dd>
                        </dl>
                    </div>
                    <div>
                        <p class="shortcut-section">Caption Editor</p>
                        <dl>
                            <dt>J</dt><dd>Next line + seek</dd>
                            <dt>K</dt><dd>Previous line + seek</dd>
                            <dt>T</dt><dd>Toggle current line</dd>
                            <dt>✂</dt><dd>Split line at word</dd>
                            <dt>⤵</dt><dd>Merge with next line</dd>
                            <dt>Ctrl+S</dt><dd>Save captions</dd>
                            <dt>Replace All</dt><dd>Fix Whisper misrecognition</dd>
                        </dl>
                        <p class="shortcut-section">General</p>
                        <dl>
                            <dt>?</dt><dd>This help</dd>
                        </dl>
                    </div>
                </div>
            </div>
        `;
        el.addEventListener("click", (ev) => { if (ev.target === el) el.remove(); });
        document.body.appendChild(el);
    }

    // ── Theme settings ──────────────────────────────────────────

    function loadSettings() {
        try {
            const raw = localStorage.getItem(SETTINGS_STORAGE_KEY);
            const saved = raw ? JSON.parse(raw) : null;
            applyTheme(saved?.theme || document.documentElement.dataset.theme || DEFAULT_THEME_ID);
        } catch (e) {
            applyTheme(document.documentElement.dataset.theme || DEFAULT_THEME_ID);
        }

        const themeSelect = $("setting-theme");
        if (themeSelect) themeSelect.addEventListener("change", saveSettings);
    }

    function saveSettings() {
        const themeSelect = $("setting-theme");
        const theme = applyTheme(themeSelect?.value || DEFAULT_THEME_ID);
        try {
            const raw = localStorage.getItem(SETTINGS_STORAGE_KEY);
            const existing = raw ? JSON.parse(raw) : {};
            localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify({ ...existing, theme }));
        } catch (e) {
            // Ignore storage errors.
        }
    }

    function resetSettings() {
        applyTheme(DEFAULT_THEME_ID);
        saveSettings();

        const btn = $("navbar-reset-btn");
        if (!btn) return;
        const labelEl = btn.querySelector(".pill-label");
        if (labelEl) labelEl.textContent = "Reset!";
        else btn.textContent = "Reset!";
        setTimeout(() => {
            const label = btn.querySelector(".pill-label");
            if (label) label.textContent = "Reset to Defaults";
            else btn.textContent = "Reset to Defaults";
        }, 1000);
    }

    // ── Shutdown ────────────────────────────────────────────────

    async function shutdownApp() {
        if (!confirm("Are you sure you want to quit the application?")) return;
        try {
            await api("/api/shutdown", { method: "POST" });
            document.body.innerHTML = "<div style='display:flex;justify-content:center;align-items:center;height:100vh;background:#0f151d;color:#d7e0eb;font-family:sans-serif;'><h2>Application has been closed. You can close this tab.</h2></div>";
        } catch (e) {
            alert("Failed to quit app");
        }
    }

    // ── Initialization ──────────────────────────────────────────

    async function init() {
        await loadStorageConfig();

        try {
            const deps = await api("/api/check-deps");
            const missing = renderDependencyStatus(deps);
            const hasMissingOptional = !deps.deno?.installed;
            const shouldOfferAutoInstall = deps.auto_install_available && (missing.length > 0 || hasMissingOptional);
            const consent = getAutoDownloadConsent();

            if (shouldOfferAutoInstall && !dependencyInstallAttempted) {
                if (consent === "allow") {
                    dependencyInstallAttempted = true;
                    await installMissingDependencies(false);
                } else if (!consent) {
                    showAutoDownloadConsentPrompt(deps);
                }
            }

            if (!shouldOfferAutoInstall || consent) {
                checkOnboardingForMode("reel");
            }
        } catch (e) {
            setDependencyBanner(
                "<strong>Cannot connect to server.</strong>",
                "Make sure the app is running.",
                true
            );
        }

        loadSettings();
        bindDependencyDropdownCloseHandlers();
        initKeyboardShortcuts();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    return {
        installMissingDependencies,
        updateYtdlp,
        installCaptioningDeps,
        onboardingNext,
        onboardingBack,
        skipOnboarding,
        restartOnboarding,
        checkOnboardingForMode,
        onboardingInstallCaptioning,
        onboardingInstallRuntime,
        chooseOutputFolder,
        applyOutputFolder,
        resetOutputFolder,
        allowDependencyAutoDownload,
        useManualDependencySetup,
        toggleSettingsPanel,
        openSettingsPanel,
        toggleShortcutHelp,
        resetSettings,
        shutdownApp,
        getDependencySnapshot() {
            return lastDeps;
        },
    };
})();
