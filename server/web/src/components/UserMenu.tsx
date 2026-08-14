import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Me, OrganizationOption } from "../types";

/**
 * Everything about *you* and your session, behind your own name.
 *
 * Replaces a standalone organisation `<select>` that sat in the header competing
 * for attention with the station picker beside it — two dropdowns side by side,
 * one of which most people can never use, because it renders only for the few
 * who belong to more than one organisation.
 *
 * Order is deliberate and is the same in both layouts: which organisation you
 * are in, then Settings, then Sign out last. Sign out is the one item here that
 * ends what you were doing, so it is furthest from the pointer's resting place
 * after opening the menu and is separated from everything above it.
 *
 * Switching organisation **reloads the page**, as the old switcher did, and for
 * the same reason: the organisation reaches into the station list, every
 * telemetry buffer, the map configuration, the socket's authorised groups and
 * the audio pipeline, and the server revokes the old session as part of the
 * switch. Re-bootstrapping from scratch is the only version of this with no
 * possibility of one tenant's data surviving into another's view.
 */
export function UserMenu({
  me,
  displayName,
  onSettings,
  onSignOut,
}: {
  me: Me;
  /** Held by the parent so a rename in Settings shows here without a reload. */
  displayName: string;
  /** Opens whatever "settings" means in this layout — a dialog in the console,
   *  a tab on the platform dashboard. */
  onSettings: () => void;
  onSignOut: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [orgs, setOrgs] = useState<OrganizationOption[]>([]);
  const [busy, setBusy] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .organizations()
      .then(setOrgs)
      .catch(() => setOrgs([]));
  }, [me.organization_id]);

  // A menu that only closes by choosing something is a menu you get stuck in.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function switchTo(id: string) {
    if (id === me.organization_id || busy) return;
    setBusy(true);
    try {
      await api.switchOrganization(id);
      window.location.reload();
    } catch {
      setBusy(false);
    }
  }

  // Only worth listing when there is a choice; one organisation is not a menu.
  const switchable = orgs.length > 1;

  return (
    <div className="user-menu" ref={wrap}>
      <button
        type="button"
        className={`user-menu-trigger${open ? " open" : ""}`}
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={me.email}
      >
        <span className="who">{displayName}</span>
        <span className="user-menu-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <div className="user-menu-panel" role="menu">
          {switchable && (
            <>
              <div className="user-menu-label">Organisation</div>
              {orgs.map((o) => (
                <button
                  key={o.id}
                  type="button"
                  role="menuitem"
                  className={`user-menu-item${
                    o.id === me.organization_id ? " current" : ""
                  }`}
                  disabled={busy}
                  onClick={() => void switchTo(o.id)}
                  title={
                    o.is_member
                      ? undefined
                      : "You would be working inside this organisation as a platform administrator"
                  }
                >
                  {/* Nothing beside the name. Where you ARE is said by the
                      row's own background (styles.css), and that a tenancy is
                      reached through platform access is said by the title
                      attribute above — a pill and a tick in the same corner
                      were three marks competing to say two things. */}
                  <span className="user-menu-name">{o.name}</span>
                </button>
              ))}
              <div className="user-menu-sep" />
            </>
          )}

          <button
            type="button"
            role="menuitem"
            className="user-menu-item"
            onClick={() => {
              setOpen(false);
              onSettings();
            }}
          >
            Settings
          </button>

          <div className="user-menu-sep" />
          <button
            type="button"
            role="menuitem"
            className="user-menu-item sign-out"
            onClick={() => {
              setOpen(false);
              onSignOut();
            }}
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
