import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useFitScale } from "./useFitScale";

/**
 * The fit-scale sets an inline font-size on :root — a global, not a local.
 *
 * That is the right mechanism (see the hook's own note on why it beats a
 * transform), but a global written by one view has to be cleaned up by it. The
 * console is not the only view: the platform dashboard renders with no fit-scale
 * and every rem in it — the shared header height, type sizes, chart gutters —
 * resolves against whatever :root says. Left behind, the console's solve follows
 * the operator into a layout it was not solved for, and the same markup renders
 * at a different size depending on which view happened to be open first.
 */

function Harness({ enabled = true }: { enabled?: boolean }) {
  const fit = useFitScale({ enabled, ready: true });
  return (
    <div ref={fit.outerRef}>
      <div ref={fit.innerRef}>console</div>
    </div>
  );
}

const rootSize = () => document.documentElement.style.fontSize;

afterEach(() => {
  cleanup();
  document.documentElement.style.removeProperty("font-size");
});

describe("useFitScale's hold on the document root", () => {
  it("gives the root back when the view unmounts", () => {
    // jsdom reports zero heights so the solve itself never runs; what is under
    // test is the release, so the size is planted directly.
    document.documentElement.style.fontSize = "17.25px";
    const { unmount } = render(<Harness />);
    unmount();
    expect(rootSize()).toBe("");
  });

  it("gives it back when it is disabled without unmounting", () => {
    // The narrow-window path: the console stops fit-scaling but stays mounted.
    document.documentElement.style.fontSize = "17.25px";
    const { rerender } = render(<Harness />);
    rerender(<Harness enabled={false} />);
    expect(rootSize()).toBe("");
  });

  it("leaves nothing behind for the next view to inherit", () => {
    // The actual sequence that was broken: console up, console gone, another
    // view renders and reads :root.
    document.documentElement.style.fontSize = "11px";
    const { unmount } = render(<Harness />);
    unmount();

    render(<div>platform dashboard</div>);
    expect(rootSize()).toBe("");
  });
});
