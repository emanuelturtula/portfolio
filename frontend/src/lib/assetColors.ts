/**
 * Each asset's colour on a chart: fixed by the asset, never by its rank or position.
 *
 * A colour that followed the sort order would repaint BTC the day KAS overtook it, and a
 * reader who learned "orange is Bitcoin" would be wrong without anything saying so. So the
 * assets this product tracks have a slot of their own, and any other asset takes the next free
 * slot in alphabetical order, which a change in value cannot reorder.
 *
 * The slots are CSS custom properties (`--series-*` in `index.css`), so each has a light and a
 * dark step. The steps are the dataviz reference palette's: orange, aqua and blue validated
 * together, adjacent pairs clear of the colour-vision-deficiency floor in both modes. Aqua sits
 * under 3:1 against the light surface, so every chart that uses it also shows its figures as
 * text, in a legend or a table.
 */

const KNOWN: Readonly<Record<string, string>> = {
  BTC: 'var(--series-orange)',
  KAS: 'var(--series-aqua)',
};

/**
 * For an asset with no slot of its own. Only one: orange, aqua and blue are the three slots
 * that stay apart from every other one, not only from their neighbours, and a donut puts any
 * two slices side by side. Measured: a fourth slot of the palette falls under the floor for
 * full colour vision against one of these three. Every asset past it is a neutral grey, and is
 * told apart by its label.
 */
const SPARE: readonly string[] = ['var(--series-blue)'];

const OTHER = 'var(--series-other)';

/**
 * The colour of each asset in `assets`, keyed by asset. Pass every asset the chart shows, so
 * that the spare slots are handed out over the same set each time.
 */
export function assetColors(assets: readonly string[]): ReadonlyMap<string, string> {
  const colors = new Map<string, string>();
  const unknown = [...new Set(assets)].filter((asset) => !(asset in KNOWN)).sort();

  for (const asset of assets) {
    const known = KNOWN[asset];
    if (known !== undefined) {
      colors.set(asset, known);
    }
  }
  unknown.forEach((asset, index) => {
    colors.set(asset, SPARE[index] ?? OTHER);
  });

  return colors;
}
