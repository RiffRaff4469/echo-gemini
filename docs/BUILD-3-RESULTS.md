# BUILD-3-RESULTS — v2 alarms/timers verification

_Client v2 = on-device alarms & timers (AlarmScheduler, AlarmService, Schedule
screens). Landed `ad4d2e8` (2026-09-06), protocol v1.1 alarm channel. This is
its verification report._

## Shipped

- Schedule UI (set one-off and repeating alarms/timers on the device itself).
- `AlarmScheduler`/`AlarmService` with exact-alarm permission and a boot
  receiver (alarms survive reboots).
- Server: Gemini alarm tools + `alarm`/`timer` display cards; confirmation
  round-trip bounded by `ALARM_ACK_TIMEOUT_S`.

## Automated results (green, 2026-09-06)

- Server pytest suite — **all passing**, including the alarm-tool coverage and
  the fake-device v2 socket path (`3eae727`).
- Client JVM tests — scheduling, persistence, snooze, calendar arithmetic
  (`./gradlew testDebugUnitTest`).

## On-device results

- APK installed on the Show as **`com.echogemini.terminal.debug`** (debug
  suffix — verify with `pm list packages | grep -i echo`).
- Hardware validation of the actual alarm **ringing** remains outstanding:
  set a timer/alarm on the Show and confirm audio. (See ROADMAP "Verify queue".)

## Rebuild / rerun

```
cd EchoTerminal
export JAVA_HOME="C:/Program Files/Eclipse Adoptium/jdk-17.0.20.101-hotspot"   # Windows PC
./gradlew testDebugUnitTest assembleDebug --no-daemon
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Notes: `compileSdk` is 34 (build-tools 33.0.2, platform android-34 present).
`EchoTerminal/local.properties` holds BOTH `sdk.dir` and the echo
`serverUrl`/`sharedSecret` keys — never overwrite it blindly. The server URL
bakes to the PC's Tailscale address before a device build.
