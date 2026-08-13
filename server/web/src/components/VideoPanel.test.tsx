import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { VideoPanel } from "./Panels";
import type { StreamState } from "../useVideoStream";

/**
 * What the video panel says when it is not showing video.
 *
 * These four messages answer four different questions and an operator does
 * something different about each: no camera is a hardware fact, offline is a
 * link fact, "no video stream attached" says the station has a camera and is
 * sending nothing, and "waiting for video" says we asked and are waiting.
 *
 * Saying the wrong one is not cosmetic. "No video stream attached" during a
 * connect — which is what switching station used to show for a second or two —
 * states the opposite of what is happening, and it is the message that would
 * send someone to check a camera that is about to appear on its own.
 */

afterEach(cleanup);

function panel(over: Partial<Parameters<typeof VideoPanel>[0]> = {}) {
  render(
    <VideoPanel
      streaming={false}
      online
      fitted
      streamState={"idle" as StreamState}
      {...over}
    />,
  );
  return document.querySelector(".video-surface")?.textContent ?? "";
}

describe("what the video panel says when there is no picture", () => {
  it("says it is waiting while the stream is connecting", () => {
    // The regression this test exists for: switching station.
    expect(panel({ streamState: "connecting" })).toContain("waiting for video");
    expect(panel({ streamState: "connecting" })).not.toContain("No video stream");
  });

  it("says the stream is absent only once it has settled with nothing", () => {
    // idle and unavailable are the states that mean "asked, and there is none".
    for (const state of ["idle", "unavailable"] as StreamState[]) {
      cleanup();
      expect(panel({ streamState: state }), state).toContain(
        "No video stream attached",
      );
    }
  });

  it("blames the link, not the camera, when the station is offline", () => {
    expect(panel({ online: false })).toContain("Station offline");
  });

  it("distinguishes a camera that is not fitted from one that is silent", () => {
    // A camera that is not fitted has not failed, and it outranks every other
    // message: there is no point waiting for a stream that cannot exist.
    const text = panel({ fitted: false, streamState: "connecting" });
    expect(text).toContain("No camera on this station");
    expect(text).not.toContain("waiting for video");
  });

  it("says nothing about a camera before the first health frame", () => {
    // `fitted` undefined is absence of evidence. It must not read as "no camera".
    expect(panel({ fitted: undefined })).not.toContain("No camera");
  });

  it("stops promising video that is never coming", async () => {
    // The stream hook retries forever, so a station whose camera never publishes
    // would sit on "waiting for video…" indefinitely if this were a plain read
    // of streamState. After the window the empty state is the honest answer.
    vi.useFakeTimers();
    render(<VideoPanel streaming={false} online fitted streamState="connecting" />);
    const text = () => document.querySelector(".video-surface")?.textContent ?? "";

    expect(text()).toContain("waiting for video");
    await act(async () => {
      vi.advanceTimersByTime(11_000);
    });
    expect(text()).toContain("No video stream attached");
    vi.useRealTimers();
  });

  it("waits again on a reconnect, not just the first time", async () => {
    // A stream that was live and dropped goes back through connecting. Whether
    // we have seen it before makes no difference to what is true now.
    vi.useFakeTimers();
    const { rerender } = render(
      <VideoPanel streaming online fitted streamState="playing" />,
    );
    rerender(<VideoPanel streaming={false} online fitted streamState="connecting" />);
    const text = () => document.querySelector(".video-surface")?.textContent ?? "";
    expect(text()).toContain("waiting for video");
    vi.useRealTimers();
  });

  it("shows the picture, not a message, once it is playing", () => {
    const text = panel({ streaming: true, streamState: "playing" });
    expect(text).toContain("live");
    expect(text).not.toContain("waiting");
    expect(text).not.toContain("No video");
  });
});
