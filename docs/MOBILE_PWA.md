# Mobile & PWA Guide

The Remote Desktop client includes dedicated support for mobile browsers and installable Progressive Web Apps (PWA), optimized for touch control, orientation handling, and low-latency interaction on phones and tablets.

---

## 1. Installing as a PWA

### Android (Chrome / Edge / Firefox)
1. Open Chrome on your mobile device.
2. Navigate to your Remote Desktop URL (e.g. `https://rd.yourdomain.com` or `http://100.x.x.x:9005`).
3. Tap the browser menu (`⋮`) and select **Install app** or **Add to Home screen**.
4. The application installs as a standalone app with a custom launcher icon and full-screen display mode.

### iOS (Safari)
1. Open Safari on iOS.
2. Navigate to your Remote Desktop URL.
3. Tap the **Share** button (`⎙`) and select **Add to Home Screen**.

---

## 2. Touch Interaction Model

- **Screen View (Default):**
  When launched, the session begins in safe view-only mode. Touch gestures on the screen will not move or click the remote host's mouse.
- **Screen Control (Explicit):**
  Switch to `Screen Control` via the top mode selector or the Floating Action Button.
- **Touch Input:**
  - Single tap: Left click at touch position with optional haptic vibration.
  - Drag / Move: Pointer tracking and drag operations.

---

## 3. Mobile Floating Action Button (FAB)

On mobile viewports ($<600\text{px}$ width), the cluttered desktop top/bottom toolbars are condensed into a touch-friendly **Floating Action Button (FAB)** in the bottom-right corner:

- **📋 Copy PC Clipboard:** Reads remote PC clipboard text into your mobile clipboard.
- **📥 Send to PC:** Pushes mobile clipboard contents to the remote PC.
- **⌨️ On-Screen Keyboard:** Toggles mobile virtual keyboard input.
- **⛶ Fullscreen:** Expands video to full-screen viewport.
- **🔄 Reconnect:** Seamlessly refreshes WebRTC connection if network drops.
- **⚡ Power Actions:** Access safe confirmation-gated power controls (Lock, Sleep, Restart, Shutdown).

---

## 4. Performance Recommendations for Mobile

- **Resolution:** Use the default `720p` (1280×720 @ 30 FPS) or `Low` (854×480 @ 20 FPS) over cellular/VPN connections.
- **Aspect Ratio:** For best visibility of widescreen PC displays, rotate your mobile device to landscape mode.
