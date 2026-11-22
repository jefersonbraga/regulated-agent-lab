import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TripPlanner } from "./TripPlanner";
import { CommandBar, SCOPE } from "./CommandBar";

const TODAY = "2026-11-21";
const map = () => screen.getByRole("group", { name: /^Rail network map/ });
const stationButton = (name: string | RegExp) => within(map()).getByRole("button", { name });
const sentence = () => screen.getByTestId("planner-sentence");

describe("TripPlanner", () => {
  it("draws the four stations and six links with typical times, and one line per city", () => {
    render(<TripPlanner onSend={vi.fn()} today={TODAY} />);
    expect(within(map()).getAllByRole("button")).toHaveLength(4);
    const img = within(map()).getByRole("img");
    expect(img).toHaveAccessibleName(expect.stringContaining("Zurich HB to Geneva 2 h 45"));
    expect(img.querySelectorAll(".network-link")).toHaveLength(6);
    const cities = screen.getByRole("list", { name: "The stations" });
    expect(cities).toHaveTextContent("Zurich HB — largest city and main rail hub");
    expect(cities).toHaveTextContent("Bern — the capital, in the centre");
    expect(cities).toHaveTextContent("Geneva — French-speaking, on Lake Geneva");
    expect(cities).toHaveTextContent("Basel — on the Rhine, by France and Germany");
    expect(sentence()).toHaveTextContent("Pick where you leave from and where you go.");
    expect(screen.getByRole("button", { name: "Find trains" })).toHaveAttribute("aria-disabled", "true");
  });

  it("picks origin then destination by click and highlights the route", async () => {
    render(<TripPlanner onSend={vi.fn()} today={TODAY} />);
    await userEvent.click(stationButton("Basel"));
    expect(stationButton(/^Basel/)).toHaveAccessibleName("Basel, leaving from here");
    expect(stationButton(/^Basel/)).toHaveAttribute("aria-pressed", "true");
    expect(sentence()).toHaveTextContent("Pick where you go.");
    await userEvent.click(stationButton("Geneva"));
    expect(stationButton(/^Geneva/)).toHaveAccessibleName("Geneva, going here");
    expect(screen.getByTestId("link-Geneva-Basel")).toHaveAttribute("data-selected", "true");
    expect(screen.getByTestId("link-Bern-Geneva")).not.toHaveAttribute("data-selected");
    expect(screen.getByTestId("planner-route")).toHaveTextContent("Basel → Geneva");
    // The default return trip: a week ahead, morning out, evening back, one adult.
    expect(sentence()).toHaveTextContent(
      "“Train from Basel to Geneva on 28 November 2026 in the morning, back on 29 November 2026 in the evening, 1 adult.”",
    );
  });

  it("works from the keyboard alone", async () => {
    const onSend = vi.fn();
    render(<TripPlanner onSend={onSend} today={TODAY} />);
    stationButton("Zurich HB").focus();
    await userEvent.keyboard("{Enter}");
    stationButton("Bern").focus();
    await userEvent.keyboard(" ");
    expect(screen.getByTestId("link-Zurich HB-Bern")).toHaveAttribute("data-selected", "true");
    await userEvent.click(screen.getByRole("button", { name: "Swap from and to" }));
    expect(screen.getByTestId("planner-route")).toHaveTextContent("Bern → Zurich HB");
    screen.getByRole("button", { name: "Find trains" }).focus();
    await userEvent.keyboard("{Enter}");
    expect(onSend).toHaveBeenCalledWith(
      "Train from Bern to Zurich HB on 28 November 2026 in the morning, back on 29 November 2026 in the evening, 1 adult.",
    );
  });

  it("switches to one way and drops the return leg", async () => {
    const onSend = vi.fn();
    render(<TripPlanner onSend={onSend} today={TODAY} />);
    await userEvent.click(stationButton("Bern"));
    await userEvent.click(stationButton("Geneva"));
    expect(screen.getByLabelText("Return date")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "One way" }));
    expect(screen.queryByLabelText("Return date")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Find trains" }));
    expect(onSend).toHaveBeenCalledWith("One way from Bern to Geneva on 28 November 2026 in the morning, 1 adult.");
  });

  it("fills the planner from a suggested trip without sending it", async () => {
    const onSend = vi.fn();
    render(<TripPlanner onSend={onSend} today={TODAY} />);
    const chips = screen.getByRole("group", { name: "Suggested trips" });
    expect(within(chips).getAllByRole("button")).toHaveLength(4);
    await userEvent.click(within(chips).getByRole("button", { name: "Zurich HB → Geneva · across the country, ~2 h 45" }));
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByTestId("planner-route")).toHaveTextContent("Zurich HB → Geneva");
    expect(stationButton(/^Zurich HB/)).toHaveAccessibleName("Zurich HB, leaving from here");
    expect(screen.getByTestId("link-Zurich HB-Geneva")).toHaveAttribute("data-selected", "true");
    expect(sentence()).toHaveTextContent("Train from Zurich HB to Geneva on 28 November 2026");
  });
});

describe("CommandBar with the planner", () => {
  it("sends the composed sentence through the normal send path, or hands it to the composer to edit", async () => {
    const onSend = vi.fn();
    render(<CommandBar onSend={onSend} busy={false} today={TODAY} />);
    expect(screen.getByTestId("composer-scope")).toHaveTextContent(SCOPE);
    expect(SCOPE).toBe("Train trips only, between Zurich HB, Bern, Geneva and Basel: one way or return, 1 to 9 passengers.");
    await userEvent.click(within(screen.getByRole("group", { name: "Suggested trips" })).getAllByRole("button")[1]);
    await userEvent.click(screen.getByRole("button", { name: "Edit as text" }));
    expect(onSend).not.toHaveBeenCalled();
    // The composer is a rich text field: the sentence lands in it as text to edit.
    expect(screen.getByLabelText("Message input")).toHaveTextContent(
      "Train from Basel to Bern on 28 November 2026 in the morning, back on 29 November 2026 in the evening, 1 adult.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Find trains" }));
    expect(onSend).toHaveBeenCalledWith(
      "Train from Basel to Bern on 28 November 2026 in the morning, back on 29 November 2026 in the evening, 1 adult.",
    );
  });

  it("locks the planner while a decision is pending", () => {
    render(<CommandBar onSend={vi.fn()} busy={false} lockedHint="Decide first" today={TODAY} />);
    for (const b of within(screen.getByRole("group", { name: /^Rail network map/ })).getAllByRole("button")) {
      expect(b).toBeDisabled();
    }
    expect(screen.getByRole("button", { name: "Find trains" })).toHaveAttribute("aria-disabled", "true");
  });
});
