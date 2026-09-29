/**
 * A text badge. The trailing space is not decoration: the badges sit in a flex container,
 * where CSS ignores it, but the row header's accessible name is built from the text nodes,
 * and without it "Unknown cost" and "Not in totals" would run together as "Unknown costNot
 * in totals".
 */
export function Badge({ children }: { readonly children: string }) {
  return (
    <>
      <span className="badge">{children}</span>{' '}
    </>
  );
}
