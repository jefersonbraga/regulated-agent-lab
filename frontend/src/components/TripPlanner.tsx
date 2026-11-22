import { useId, useState } from "react";
import { ArrowLeftRight, MapPin, Send, SquarePen } from "lucide-react";
import { Button } from "@astryxdesign/core/Button";
import { DateInput } from "@astryxdesign/core/DateInput";
import { Heading } from "@astryxdesign/core/Heading";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import {
  LINKS,
  NETWORK_STATIONS,
  SUGGESTED_TRIPS,
  formatMinutes,
  station,
  suggestionLabel,
  type StationName,
} from "../network";
import {
  MAX_PASSENGERS,
  PARTS_OF_DAY,
  addDays,
  composeRequest,
  initialPlanner,
  isoDay,
  pickStation,
  plannerProblem,
  type LegTime,
  type PartOfDay,
  type PlannerState,
  type TripType,
} from "../planner";

type ISODate = `${number}${number}${number}${number}-${number}${number}-${number}${number}`;

const VIEW_W = 320;
const VIEW_H = 260;

/** Quadratic curve control point: the long links bow away from Bern so the labels stay apart. */
const BOW: Record<string, number> = { "Zurich HB|Geneva": -34, "Geneva|Basel": -34 };

function linkPath(a: StationName, b: StationName): { d: string; mx: number; my: number } {
  const p = station(a);
  const q = station(b);
  const bow = BOW[`${a}|${b}`] ?? 0;
  const mx = (p.x + q.x) / 2;
  const my = (p.y + q.y) / 2;
  if (!bow) return { d: `M${p.x},${p.y} L${q.x},${q.y}`, mx, my };
  // Normal to the chord, scaled by the bow; the curve's midpoint is half-way to the control point.
  const len = Math.hypot(q.x - p.x, q.y - p.y);
  const nx = -(q.y - p.y) / len;
  const ny = (q.x - p.x) / len;
  const cx = mx + nx * bow * 2;
  const cy = my + ny * bow * 2;
  return { d: `M${p.x},${p.y} Q${cx},${cy} ${q.x},${q.y}`, mx: mx + nx * bow, my: my + ny * bow };
}

const sameLink = (a: StationName, b: StationName, o: StationName | null, d: StationName | null) =>
  o !== null && d !== null && ((a === o && b === d) || (a === d && b === o));

export interface NetworkMapProps {
  origin: StationName | null;
  destination: StationName | null;
  onPick: (name: StationName) => void;
  isDisabled?: boolean;
}

/** The schematic network: links with typical times drawn in SVG, stations as real buttons. */
export function NetworkMap({ origin, destination, onPick, isDisabled = false }: NetworkMapProps) {
  const summary = LINKS.map(([a, b, m]) => `${a} to ${b} ${formatMinutes(m)}`).join(", ");
  return (
    <div className="network-map" role="group" aria-label="Rail network map: pick where you leave from, then where you go">
      <svg viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} role="img" aria-label={`Typical direct travel times: ${summary}.`}>
        {LINKS.map(([a, b, minutes]) => {
          const { d, mx, my } = linkPath(a, b);
          const on = sameLink(a, b, origin, destination);
          return (
            <g key={`${a}|${b}`} className="network-link" data-selected={on || undefined} data-testid={`link-${a}-${b}`}>
              <path d={d} />
              <text x={mx} y={my} dy="0.35em" textAnchor="middle">
                {formatMinutes(minutes)}
              </text>
            </g>
          );
        })}
      </svg>
      {NETWORK_STATIONS.map((s) => {
        const role = s.name === origin ? "from" : s.name === destination ? "to" : null;
        return (
          <button
            key={s.name}
            type="button"
            className="network-station"
            data-side={s.labelSide}
            data-role={role ?? undefined}
            style={{ left: `${(s.x / VIEW_W) * 100}%`, top: `${(s.y / VIEW_H) * 100}%` }}
            aria-pressed={role !== null}
            aria-label={`${s.name}${role ? `, ${role === "from" ? "leaving from here" : "going here"}` : ""}`}
            disabled={isDisabled}
            onClick={() => onPick(s.name)}
          >
            <span className="network-dot" aria-hidden="true" />
            <span className="network-name" aria-hidden="true">
              {s.name}
              {role && <span className="network-role">{role}</span>}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function LegTimeField({
  label,
  value,
  onChange,
  isDisabled,
}: {
  label: string;
  value: LegTime;
  onChange: (t: LegTime) => void;
  isDisabled: boolean;
}) {
  return (
    <div className="planner-time">
      <Selector
        label={label}
        options={PARTS_OF_DAY}
        value={value.part}
        onChange={(part) => onChange({ ...value, part: part as PartOfDay })}
        isDisabled={isDisabled}
        size="sm"
      />
      {value.part === "after" && (
        <TextInput
          label={`${label}: after (HH:MM)`}
          isLabelHidden
          value={value.after}
          onChange={(after) => onChange({ ...value, after })}
          placeholder="17:00"
          isDisabled={isDisabled}
          size="sm"
          width={96}
        />
      )}
    </div>
  );
}

export interface TripPlannerProps {
  /** Sends the composed sentence through the normal request path. */
  onSend: (text: string) => void;
  /** Puts the sentence in the free-text composer to edit before sending. */
  onEdit?: (text: string) => void;
  isDisabled?: boolean;
  /** Today's local date (YYYY-MM-DD); injectable for tests. */
  today?: string;
}

/** A guided way to ask for a trip, for people who do not know the country's stations. */
export function TripPlanner({ onSend, onEdit, isDisabled = false, today = isoDay(new Date()) }: TripPlannerProps) {
  const [s, setS] = useState<PlannerState>(() => initialPlanner(today));
  const headingId = useId();
  const update = (patch: Partial<PlannerState>) => setS((prev) => ({ ...prev, ...patch }));
  const sentence = composeRequest(s);
  const problem = plannerProblem(s, today);
  const ready = sentence !== null && problem === null;

  function setDepart(value: string | undefined) {
    const departDate = value ?? "";
    // Keep the return on or after the departure, so the form never composes an impossible trip.
    const returnDate = departDate && s.returnDate < departDate ? addDays(departDate, 1) : s.returnDate;
    update({ departDate, returnDate });
  }

  return (
    <section className="planner" aria-labelledby={headingId} data-testid="trip-planner">
      <div className="planner-head">
        <Heading level={2} id={headingId} className="planner-title">
          Plan a trip
        </Heading>
        <Text type="supporting" color="secondary">
          Tap a station to leave from, then one to go to. Times on the lines are typical journey times.
        </Text>
      </div>

      <div className="planner-suggest" role="group" aria-label="Suggested trips">
        {SUGGESTED_TRIPS.map((t) => (
          <Button
            key={t.id}
            label={suggestionLabel(t)}
            size="sm"
            variant="secondary"
            icon={<MapPin size="1em" aria-hidden="true" />}
            isDisabled={isDisabled}
            onClick={() => update({ origin: t.origin, destination: t.destination })}
          />
        ))}
      </div>

      <div className="planner-body">
        <div className="planner-map">
          <NetworkMap
            origin={s.origin}
            destination={s.destination}
            onPick={(name) => update(pickStation(s, name))}
            isDisabled={isDisabled}
          />
          <ul className="planner-cities" aria-label="The stations">
            {NETWORK_STATIONS.map((c) => (
              <li key={c.name}>
                <Text type="supporting" weight="semibold">
                  {c.name}
                </Text>
                <Text type="supporting" color="secondary">
                  {" "}
                  — {c.blurb}
                </Text>
              </li>
            ))}
          </ul>
        </div>

        <div className="planner-form">
          <div className="planner-route" data-testid="planner-route">
            <Text weight="semibold">
              {s.origin ?? "From ?"} → {s.destination ?? "To ?"}
            </Text>
            <Button
              label="Swap from and to"
              size="sm"
              variant="ghost"
              isIconOnly
              icon={<ArrowLeftRight size="1em" aria-hidden="true" />}
              isDisabled={isDisabled || !s.origin || !s.destination}
              onClick={() => update({ origin: s.destination, destination: s.origin })}
            />
          </div>
          <SegmentedControl
            label="Trip type"
            value={s.tripType}
            onChange={(v) => update({ tripType: v as TripType })}
            isDisabled={isDisabled}
            size="sm"
            layout="fill"
          >
            <SegmentedControlItem value="one-way" label="One way" />
            <SegmentedControlItem value="return" label="Return" />
          </SegmentedControl>
          <div className="planner-leg">
            <DateInput
              label="Departure date"
              value={(s.departDate || undefined) as ISODate | undefined}
              onChange={setDepart}
              min={today as ISODate}
              isDisabled={isDisabled}
              size="sm"
            />
            <LegTimeField
              label="Leaving"
              value={s.outbound}
              onChange={(outbound) => update({ outbound })}
              isDisabled={isDisabled}
            />
          </div>
          {s.tripType === "return" && (
            <div className="planner-leg">
              <DateInput
                label="Return date"
                value={(s.returnDate || undefined) as ISODate | undefined}
                onChange={(v) => update({ returnDate: v ?? "" })}
                min={(s.departDate || today) as ISODate}
                isDisabled={isDisabled}
                size="sm"
              />
              <LegTimeField
                label="Coming back"
                value={s.back}
                onChange={(back) => update({ back })}
                isDisabled={isDisabled}
              />
            </div>
          )}
          <NumberInput
            label="Passengers"
            description={`Adults, 1 to ${MAX_PASSENGERS}`}
            value={s.passengers}
            onChange={(passengers) => update({ passengers })}
            min={1}
            max={MAX_PASSENGERS}
            isIntegerOnly
            hasNumberSteppers
            isDisabled={isDisabled}
            size="sm"
            width={140}
          />
        </div>
      </div>

      <div className="planner-send">
        <p className="planner-sentence" aria-live="polite" data-testid="planner-sentence">
          {ready ? (
            <>
              <Text type="supporting" color="secondary">
                You can also type this:{" "}
              </Text>
              <Text type="supporting">“{sentence}”</Text>
            </>
          ) : (
            <Text type="supporting" color="secondary">
              {problem}
            </Text>
          )}
        </p>
        <div className="planner-actions">
          {onEdit && (
            <Button
              label="Edit as text"
              size="sm"
              variant="ghost"
              icon={<SquarePen size="1em" aria-hidden="true" />}
              isDisabled={isDisabled || !ready}
              onClick={() => sentence && onEdit(sentence)}
            />
          )}
          <Button
            label="Find trains"
            variant="primary"
            icon={<Send size="1em" aria-hidden="true" />}
            isDisabled={isDisabled || !ready}
            onClick={() => sentence && ready && onSend(sentence)}
          />
        </div>
      </div>
    </section>
  );
}
