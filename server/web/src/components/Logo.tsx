/** The brand mark plus wordmark.
 *
 * The mark is the supplied logo.png (166x166 RGBA, transparent background),
 * referenced by path rather than inlined so swapping the asset stays a file
 * drop. A vector export would be better - 166px is comfortable at the 24px and
 * 56px sizes used here but leaves no headroom for anything larger.
 */
export function Logo({ size = "small" }: { size?: "small" | "large" }) {
  return (
    <div className={`brand ${size}`}>
      <img src="/logo.png" alt="" className="brand-mark" />
      <span className="brand-word">PERCEPTA</span>
    </div>
  );
}
