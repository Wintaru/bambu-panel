// Kiosk profile for the printer panel
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.sessionstore.max_resumed_crashes", 0);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.translations.automaticallyPopup", false);
user_pref("app.update.auto", false);
// touch: no pinch zoom, no overscroll bounce, no swipe-to-navigate
user_pref("apz.allow_zooming", false);
user_pref("apz.overscroll.enabled", false);
user_pref("browser.gesture.swipe.left", "");
user_pref("browser.gesture.swipe.right", "");
user_pref("browser.gesture.pinch.in", "");
user_pref("browser.gesture.pinch.out", "");
user_pref("widget.disable-swipe-tracker", true);
user_pref("dom.w3c_touch_events.enabled", 1);
user_pref("ui.popup.disable_autohide", false);
// video
user_pref("media.autoplay.default", 0);
user_pref("media.ffmpeg.vaapi.enabled", true);
