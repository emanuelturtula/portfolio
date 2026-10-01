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

/**
 * A badge that is a link, for the one marker that has somewhere to go: "Held exceeds history"
 * takes the reader to the holdings check that lists the asset. Same trailing space, same
 * reason as {@link Badge}.
 */
export function BadgeLink({
  href,
  children,
}: {
  readonly href: string;
  readonly children: string;
}) {
  return (
    <>
      <a className="badge" href={href}>
        {children}
      </a>{' '}
    </>
  );
}
