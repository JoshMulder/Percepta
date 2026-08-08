import type { ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * A modal popout: a full-viewport scrim with a dialog box centred on it.
 * Clicking the scrim closes it; clicking the box does not.
 *
 * **It renders through a portal for a reason, not out of habit.** These popouts
 * are opened from inside sensor panels, and a panel showing "disconnected" is
 * wrapped by PanelState in `.panel-state-content`, which is dimmed to opacity
 * 0.35 (and aria-hidden). CSS opacity on an ancestor establishes a group that
 * no descendant can climb back out of — so a popout rendered in the panel's own
 * subtree opened at 0.35, behind its opaque siblings: it was there, but a ghost.
 * Portaling to document.body lifts the dialog out of that dimmed subtree so it
 * opens at full strength whatever state the panel it belongs to is in.
 *
 * The click handlers ride the React tree, not the DOM tree, so onClose still
 * fires across the portal boundary exactly as it would inline.
 */
export function Popout({
  onClose,
  label,
  className,
  children,
}: {
  onClose: () => void;
  /** Names the dialog for assistive tech, e.g. "Battery history". */
  label: string;
  /** Extra class on the box, e.g. "transcript-detail" for the wider variant. */
  className?: string;
  children: ReactNode;
}) {
  return createPortal(
    <div className="power-detail-scrim" onClick={onClose} role="presentation">
      <div
        className={className ? `power-detail ${className}` : "power-detail"}
        role="dialog"
        aria-label={label}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
