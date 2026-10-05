import { useId, type CSSProperties, type ReactNode } from 'react';

import { barLength, formatMoney, maxMoney, type FormatMoneyOptions, type Money } from '@/lib/money';

/**
 * How a bar is painted: in its row's colour at full strength, or as a soft wash of the same
 * colour. Two shades of one hue, never two hues: the colour of a row is its asset's, the same
 * one the donut and every swatch use, and the shade is what tells the two series apart.
 */
export type BarShade = 'solid' | 'soft';

export interface BarSeries {
  readonly name: string;
  readonly shade: BarShade;
}

export interface Bar {
  readonly shade: BarShade;
  /** `null` is a figure that does not exist, such as the value of something unpriced: a dash. */
  readonly value: Money | null;
}

export interface BarRow {
  readonly key: string;
  readonly label: ReactNode;
  /**
   * The row's identity colour, a CSS colour such as `var(--series-orange)`. Without one, the
   * row is drawn in the neutral grey every asset past the palette's slots takes.
   */
  readonly color?: string | undefined;
  readonly bars: readonly Bar[];
  /** Under the label: a figure about the row as a whole, such as its return. */
  readonly aside?: ReactNode;
}

interface BarChartProps {
  /** What the chart shows, for a screen reader: the card's heading is the visible title. */
  readonly title: string;
  /** The legend. Left out for a single series, whose heading already says what is drawn. */
  readonly series: readonly BarSeries[];
  readonly rows: readonly BarRow[];
  /** How the figure at the end of each bar is written. */
  readonly format: FormatMoneyOptions;
}

interface BarTrackProps {
  readonly bar: Bar;
  /** The longest bar on the chart: every length is a share of it. */
  readonly max: Money;
  readonly format: FormatMoneyOptions;
}

function BarTrack({ bar, max, format }: BarTrackProps) {
  if (bar.value === null) {
    return (
      <div className="bar-track">
        <span className="bar-value">—</span>
      </div>
    );
  }

  const length = barLength(bar.value, max);
  return (
    <div
      className={`bar-track bar-${bar.shade}`}
      style={{ '--bar-length': length } as CSSProperties}
    >
      {length !== '0%' && <span className="bar" />}
      <span className="bar-value">{formatMoney(bar.value, format)}</span>
    </div>
  );
}

/**
 * Horizontal bars, one row per item and one bar per series, every bar on the same scale from
 * zero. Horizontal because a phone has width to spare for a bar and none for a column of
 * labels, and because an asset's name reads left to right beside its bar.
 *
 * Every bar carries its figure at its tip, as text in the page's ink, so no reading depends on
 * a colour or on hovering. The lengths come from {@link barLength}, which works in decimal: the
 * stylesheet receives a percentage as a string, and no amount becomes a float on the way.
 *
 * Like the donut, the plot is `aria-hidden`: the table beside every chart carries the same
 * figures in a form a screen reader can walk, and a figure read twice is noise. The caption and
 * the legend are not hidden, and the figure is named by its caption explicitly: not every
 * screen reader derives a figure's name from a `<figcaption>` on its own.
 */
export function BarChart({ title, series, rows, format }: BarChartProps) {
  const captionId = useId();
  const max = maxMoney(
    rows.flatMap((row) => row.bars.flatMap((bar) => (bar.value === null ? [] : [bar.value]))),
  );

  return (
    <figure className="bar-chart" aria-labelledby={captionId}>
      <figcaption id={captionId} className="visually-hidden">
        {title}
      </figcaption>
      {series.length > 1 && (
        <ul className="legend bar-legend">
          {series.map((entry) => (
            <li key={entry.name}>
              <span className={`swatch swatch-${entry.shade}`} aria-hidden="true" />
              {entry.name}
            </li>
          ))}
        </ul>
      )}
      <div className="bar-rows" aria-hidden="true">
        {rows.map((row) => (
          <div
            key={row.key}
            className="bar-row"
            style={{ '--bar-color': row.color } as CSSProperties}
          >
            <div className="bar-label">
              <span className="bar-name">
                <span className="swatch" />
                {row.label}
              </span>
              {row.aside}
            </div>
            <div className="bar-tracks">
              {row.bars.map((bar, index) => (
                // A row's bars are its series, in the legend's order: the index is the series.
                <BarTrack key={index} bar={bar} max={max} format={format} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </figure>
  );
}
