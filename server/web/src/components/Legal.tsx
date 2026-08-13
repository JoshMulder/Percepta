/**
 * Privacy Policy and Terms of Use, reachable from the login page.
 *
 * **This text is a DRAFT placeholder and has not been reviewed by anyone
 * qualified to write it.** It was written to fill the gap so the links exist and
 * the pages render, and it describes what this system actually does — which is
 * the useful half. The obligations it states are plausible, not authoritative:
 * before this is shown to a customer it needs a lawyer's pass, and the bracketed
 * placeholders below need real values.
 *
 * Deliberately no data collection of its own: these are static pages, reachable
 * signed-out, and they load nothing from anywhere. A privacy policy that phoned
 * a third party to render would be a poor advertisement for one.
 */

const COMPANY = "[Company name]";
const CONTACT = "[privacy@example.com]";
const UPDATED = "13 August 2026";

export function Legal({
  page,
  onBack,
}: {
  page: "privacy" | "terms";
  onBack: () => void;
}) {
  return (
    <div className="legal">
      <article className="legal-card">
        <p className="legal-draft">
          <b>Draft.</b> This text is a placeholder pending legal review and is
          not a binding statement of {COMPANY}'s obligations.
        </p>
        {page === "privacy" ? <Privacy /> : <Terms />}
        <p className="legal-updated">Last updated {UPDATED}.</p>
        <button type="button" className="btn ghost" onClick={onBack}>
          Back to sign in
        </button>
      </article>
    </div>
  );
}

function Privacy() {
  return (
    <>
      <h1>Privacy Policy</h1>
      <p>
        Percepta is an operations console for remote ground stations. It is used
        by named operators on behalf of the organisation that owns the equipment,
        and this policy describes what it records about those people and about
        the sites they monitor.
      </p>

      <h2>What we hold about you</h2>
      <p>
        For each account: your name, email address, the organisation you belong
        to, your role within it, and — if your organisation requires a second
        factor — the secret your authenticator app was set up with. Your password
        is stored only as a one-way hash and cannot be read back by us.
      </p>
      <p>
        We also keep an audit record of actions that change something or command
        hardware: signing in, changing permissions, tuning a receiver, operating a
        floodlight, updating a station's software. Each entry records who, what,
        when, and the address the request came from. These exist so an
        organisation can answer "who moved it" about equipment that is often
        unattended, and they are kept even after an account is deleted, with the
        email address retained as text so historic entries remain meaningful.
      </p>

      <h2>What the stations record</h2>
      <p>
        A ground station reports telemetry about itself and its surroundings:
        weather, power, aircraft transponder broadcasts, radio activity, and video
        from any fitted camera. This is data about a place and its airspace rather
        than about identifiable people, but a camera at a site may incidentally
        record individuals, and aircraft identifiers can be associated with an
        operator. Deployments carrying a camera should satisfy themselves that
        their site signage and lawful basis are in order.
      </p>

      <h2>Where it goes</h2>
      <p>
        Telemetry and recordings are held by the platform operated for your
        organisation. Map imagery is proxied through that platform rather than
        fetched by your browser, so opening a station does not disclose its
        location to a mapping provider. Data is not sold, and is not shared with
        third parties except where required by law or where you have asked us to.
      </p>

      <h2>How long it is kept</h2>
      <p>
        Local recordings on a station are pruned by age and by disk budget, both
        site-configurable. Platform-side history is retained while the
        organisation remains active. Audit entries are retained for the life of
        the deployment, for the reason given above.
      </p>

      <h2>Your choices</h2>
      <p>
        You can change your own name, email address and password from Settings.
        Requests to see, correct or delete the personal data held about you should
        go to your organisation's administrator in the first instance, or to{" "}
        {CONTACT}. Deleting an account removes the person; it does not rewrite the
        audit history of what that account did.
      </p>

      <h2>Cookies</h2>
      <p>
        One session cookie, set when you sign in, marked HttpOnly so that page
        scripts cannot read it. No advertising or analytics cookies are set. Your
        browser also stores display preferences locally; these never leave your
        machine.
      </p>
    </>
  );
}

function Terms() {
  return (
    <>
      <h1>Terms of Use</h1>
      <p>
        These terms govern use of the Percepta console. Access is granted by the
        organisation that owns the equipment you are monitoring, and using the
        console means accepting what follows.
      </p>

      <h2>Your account</h2>
      <p>
        Accounts are personal. Do not share credentials or let somebody else act
        under your account: the audit trail attributes every command to the
        signed-in user, and that record is only as truthful as the account it
        names. Tell your administrator promptly if you believe your credentials
        have been compromised.
      </p>

      <h2>Operating equipment</h2>
      <p>
        Some controls have physical effect at a remote site — moving a camera,
        tuning a receiver, switching a floodlight, replacing a station's software.
        Use them only for the purpose your organisation intends, and only where
        you are competent and authorised to do so. Radio transmission, where it
        exists, may only be used by someone appropriately licensed and within the
        conditions of that licence.
      </p>

      <h2>What the console is not</h2>
      <p>
        Percepta is a monitoring and operations tool. It is <b>not</b> an air
        traffic control system, not a navigation or separation service, and not a
        safety-of-life system. Aircraft positions are received opportunistically
        from transponder broadcasts and may be incomplete, delayed or absent.
        Weather readings come from site instruments and are not an aviation
        forecast. Do not rely on it for any decision affecting the safety of an
        aircraft, a vessel or a person.
      </p>

      <h2>Availability</h2>
      <p>
        Remote sites lose power and connectivity, and the console reports what a
        station last said rather than guaranteeing it is current. We make no
        promise of uninterrupted availability, and a station may be offline
        without notice.
      </p>

      <h2>Acceptable use</h2>
      <p>
        Do not attempt to reach data belonging to another organisation, probe or
        interfere with the platform's security, or use recordings to monitor
        individuals. Access may be suspended where use breaches these terms.
      </p>

      <h2>Liability</h2>
      <p>
        To the extent permitted by law, {COMPANY} is not liable for loss arising
        from reliance on data presented by the console, or from a station being
        unavailable. Nothing here excludes liability that cannot lawfully be
        excluded.
      </p>

      <h2>Changes</h2>
      <p>
        These terms may change as the product does. Material changes will be
        notified to organisation administrators. Questions go to {CONTACT}.
      </p>
    </>
  );
}
