import type { SchedulerStatus } from '@/api/health';
import {
  NO_TICK_WORDS,
  TICK_FAILED_WORDS,
  TICK_SUCCEEDED_WORDS,
  TIMER_NAMES,
  TIMER_STATE_WORDS,
} from '@/lib/health';
import { HealthSection, InstantOrNone } from '@/pages/health/HealthSection';

const TIMERS_HEADING_ID = 'timers-heading';

interface TimersSectionProps {
  readonly timers: readonly SchedulerStatus[];
}

/** The last tick and how it ended: what a timer that is switched on has to report. */
function TickRows({ timer }: { readonly timer: SchedulerStatus }) {
  return (
    <>
      <dt>Last tick</dt>
      <dd>
        <InstantOrNone value={timer.last_tick_at} none={NO_TICK_WORDS} />
      </dd>
      {timer.last_tick_succeeded !== null && (
        <>
          <dt>Last result</dt>
          <dd>{timer.last_tick_succeeded ? TICK_SUCCEEDED_WORDS : TICK_FAILED_WORDS}</dd>
        </>
      )}
    </>
  );
}

function TimerItem({ timer }: { readonly timer: SchedulerStatus }) {
  return (
    <div>
      <h4>{TIMER_NAMES[timer.name]}</h4>
      <dl className="health-details">
        <dt>State</dt>
        <dd>{TIMER_STATE_WORDS[timer.state]}</dd>
        {timer.state !== 'disabled' && <TickRows timer={timer} />}
      </dl>
    </div>
  );
}

/**
 * The five timers: the balance sync, the price refresh, the price backfill, the balance rebuild
 * and the backup. Each shows its state in words, when it last finished a tick and how that tick
 * ended. A timer that never ticked says so ("none since the server started": the record is in
 * memory). A timer that is `disabled` is told apart from one that is `stopped` and shows its
 * state only, since a switched-off timer never ticks (spec 030, R11). The backend always lists
 * all five, so there is no empty state. See docs/specs/030-observability.md.
 */
export function TimersSection({ timers }: TimersSectionProps) {
  return (
    <HealthSection headingId={TIMERS_HEADING_ID} title="Timers">
      {timers.map((timer) => (
        <TimerItem key={timer.name} timer={timer} />
      ))}
    </HealthSection>
  );
}
