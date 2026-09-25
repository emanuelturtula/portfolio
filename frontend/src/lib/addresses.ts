/**
 * Truncates an address for display, per the spec's "Addresses: truncated, copyable, never
 * in a URL" section.
 *
 * Never used to build a route, a query string or a log line - wallets are addressed by id
 * everywhere else in this codebase, and this module exists only to shorten what `<Address>`
 * puts on screen.
 */

const ELLIPSIS = '…';

/**
 * With a `:` (Kaspa's `kaspa:`/`kaspatest:` prefix), keeps the prefix, the `:` and 6
 * characters after it, then an ellipsis, then the address's last 6 characters. Without one,
 * keeps the first 8 characters, an ellipsis, then the last 6. An address short enough that
 * this would not shorten it - `head + tail` at least as long as the address itself - is
 * returned whole.
 */
export function truncateAddress(address: string): string {
  const colonIndex = address.indexOf(':');
  const headLength = colonIndex === -1 ? 8 : colonIndex + 1 + 6;
  const tailLength = 6;

  if (address.length <= headLength + tailLength) {
    return address;
  }

  return `${address.slice(0, headLength)}${ELLIPSIS}${address.slice(-tailLength)}`;
}
