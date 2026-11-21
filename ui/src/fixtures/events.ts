/**
 * Run event streams for tests and the dev-only demo mode (src/demo.ts). The poisoned round trip
 * is the stream POST /runs/stream and POST /runs/{run_id}/approval/stream send for the mock API
 * (LLM_PROVIDER=mock, TRAVEL_SEARCH_POISONED=1) in graph order: planner, pricing (the offer),
 * approval, then booking holds the chosen seats and pays. The other scenarios derive from it.
 */
import type { AuditEntry, Leg, Offer, Option, Quote, RunEvent, RunView } from "../types";
import { poisonedAudit } from "./audit";

export const RUN_ID = "demo-run";

/** Simulated Demo Rail train between Zurich HB and Bern (direct IC, 56 min), as agents/pricing.py offers it. */
function option(leg: Leg, day: string, hour: number): Option {
  const [from, to] = leg === "outbound" ? ["ZUE", "BER"] : ["BER", "ZUE"];
  const hh = String(hour).padStart(2, "0");
  const train_id = `DR-${from}-${to}-${day.replace(/-/g, "")}-${hh}02`;
  // Off-peak trains (not 06-08 or 16-18) also have the 2nd-class saver fare, half the standard.
  const saver = ![6, 7, 8, 16, 17, 18].includes(hour);
  return {
    leg,
    train_id,
    depart: `${day}T${hh}:02:00`,
    arrive: `${day}T${hh}:58:00`,
    fare_id: saver ? `${train_id}-2nd-saver` : `${train_id}-2nd`,
    price_cents: saver ? 2800 : 5600,
    fare_type: saver ? "saver" : "standard",
    product: "IC",
  };
}

const DEFAULT_WINDOW = { earliest: "07:00:00", latest: null };
/** "in the morning" on the way out (agents/fixtures/mock_roundtrip.json); no time on the way back. */
const MORNING = { earliest: "06:00:00", latest: "12:00:00" };
const hours = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => from + i);
/** Every train in the window, earliest first, at most 12 per leg (agents/pricing.py TRAINS_PER_LEG). */
const outboundOptions = hours(6, 11).map((h) => option("outbound", "2026-12-01", h));
const returnOptions = hours(7, 18).map((h) => option("return", "2026-12-02", h));

/** Far enough ahead that the deadline chip never turns into a countdown in a demo or test. */
export const APPROVE_BEFORE = "2099-12-01T06:00:00+00:00";

/**
 * Every train per leg in its window: the whole morning out (06:02-11:02, 6 trains), from 07:00
 * back (no time given, capped at 12: 07:02-18:02). The cheapest, 09:02 saver both ways (ties go to
 * the shortest journey, then the earliest), is recommended.
 */
export const offer: Offer = {
  legs: [
    {
      leg: "outbound",
      window: MORNING,
      window_defaulted: false,
      options: outboundOptions,
      recommended_fare_id: outboundOptions[3].fare_id,
    },
    {
      leg: "return",
      window: DEFAULT_WINDOW,
      window_defaulted: true,
      options: returnOptions,
      recommended_fare_id: returnOptions[2].fare_id,
    },
  ],
  passengers: 1,
  currency: "CHF",
  expires_at: APPROVE_BEFORE,
};

/**
 * agents.state.compute_quote_hash of each selection (outbound fare id | return fare id), computed
 * with the Python models, so the demo answers POST /runs/{run_id}/quote like the API.
 */
export const QUOTE_HASHES: Record<string, string> = {
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-0702-2nd": "b7365d9507cc34ddddc6882c2e619aec8c53efded56fc9a649df465e97e6f542",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-0802-2nd": "15c33e1cced78712e24cd05b29c5f899316dd08d8ecba9d75e4cbc2815eae82a",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-0902-2nd-saver": "8150b68fe8c866eef7ad5b9194a2ca250122e08d9b15b24f229f2a3a37ddbc8c",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-1002-2nd-saver": "f6be5ac0044ecc8737540e13c18a0ef932c1fefcb59c10ec36b763d210ad8d2d",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-1102-2nd-saver": "f4a7b72de40affcf4f4b6da5337bccd27b2caa85b686154b5e99dbb48d74204b",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-1202-2nd-saver": "9172328e44b6b535b09585ed4e9adcb1bce666f133be8f362fe95a25fcc42662",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-1302-2nd-saver": "20156a609dcd4bc3e85f015145783ea100d749499c1623b1a550fdfd13f3aa19",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-1402-2nd-saver": "2c2dd2e9de4d6ef160c146add8ace92eaa798f61b0ec6349a63889463ad2c960",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-1502-2nd-saver": "96d3f0cc8073250f1f4d6d8b3f28d3c5de894bc6018d7eef09672ae378f3b9f6",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-1602-2nd": "875edb4edaee53699c4b2d39a61879aea65c397d5cd2306cad6c846cec7f6e00",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-1702-2nd": "c1e7eba2a04ffe180ffc50627aa39de533e12955592e1d3a9d8c73a8777c096c",
  "DR-ZUE-BER-20261201-0602-2nd|DR-BER-ZUE-20261202-1802-2nd": "32f15b58f723368c5cd90ffa04dcb3a2fb7ecd8cdb1dd7bab8ac19324513b68a",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-0702-2nd": "f09f5dbd0bca62c2d6c82e12c041b1cb35ddf7832844e9fda053294a2785c3f9",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-0802-2nd": "92dd80032d6cb951d7a66fc80e5efc774aba83b334049b561041cb47d58c4de1",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-0902-2nd-saver": "90d43bc84ceb19c29d092e71918cf275dd5c4ebb32e339657dbd96ecd78ade29",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-1002-2nd-saver": "816f7bdf4dd07408a0928e2438ee6eb6cabd64333b2e66b258667dc2894e00e0",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-1102-2nd-saver": "58b692f082211983cb9281db3cea2efbb3a3e203cf3e702a18393fc199de2b8f",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-1202-2nd-saver": "2aa07adaf08941885bab9da9eb8ccca872f203b4ddb0ac7c8e7079752c13c607",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-1302-2nd-saver": "4aa7850794197c62b9c68ccc49c7a6f1b1d83166f79d9a97d3e9e03b2eddf165",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-1402-2nd-saver": "bbe8318d841ba56cf0f1ebaa37ea12c5eb47ac8c03d06e5a25a6310ecaf7ab66",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-1502-2nd-saver": "f8b7a60018d5a54b725c5887c1665a4daaa0c376ac8f32919b50a284df1c4302",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-1602-2nd": "22c0335dacc09c71337fa41b3991ba1840495a9e58b5be0c5c5acc91d2500bf9",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-1702-2nd": "008139f6917e3dc85df79885c83fd698be005befcfb6cd8eec10c4c9293e6f96",
  "DR-ZUE-BER-20261201-0702-2nd|DR-BER-ZUE-20261202-1802-2nd": "3258a6bd229eeca50398ada45f2d2ac1fb9040a40158aef516a388e0d39f1a4b",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-0702-2nd": "aca5171c2ea0b011dcc71fd4ce80a4e12ece598d80f8a8e232deb949a8f653e0",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-0802-2nd": "6607e1ff8c678058e7de0ba3e7e7dd939a6814cdb4088ceb8c1fbb76ca55ad88",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-0902-2nd-saver": "01e6e6323e8a7e80f4848bbafe88899ab8d1ad9a34a7566baf8544cd66b5771b",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-1002-2nd-saver": "c35df6aba828a17482f2019de21734a27f477d5ae15d0be6fb7cd1057b6c493c",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-1102-2nd-saver": "95a8a8b63da26208edfdd8feb93e5d35a18852e29c7d9c0ee2c9022f5b1e5f9f",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-1202-2nd-saver": "2e0033a6b53c18046c2e13da9ff58fe2413ff99c49998653deab0b71c6144e0c",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-1302-2nd-saver": "45194304820e26492eb7780f29f65396911b7bbd12eb8200a8c81ae328128ad3",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-1402-2nd-saver": "0d05c4350c13f105fffe12cd9768eb3c99831ae8ab79b8da365ce2e512246f5f",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-1502-2nd-saver": "6de53efc410514b7fdfd9fdf133f6d23655598294dbb434e3c01279e2d5a7d02",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-1602-2nd": "ec5b102e3de3baa1a33245ae6081cbc3d541a68306394098f8217d21ff0264f6",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-1702-2nd": "626137ab9cedcc0b8a0b325e64f9d5819cda600b22556d5e2deecf3d885dd0b1",
  "DR-ZUE-BER-20261201-0802-2nd|DR-BER-ZUE-20261202-1802-2nd": "48575d55db5c8d43fd752b442f0f98763e62fdc91a9b2734f6c48ed6ee942959",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-0702-2nd": "f1d430f6168c90a084830a6cf7b90c6984103a9b5fed65de32b23ae79506a248",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-0802-2nd": "e60227621605105e8968328f84bc22e4d85b55a7a318dcb43836a030333f4c0b",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-0902-2nd-saver": "39c313e3ba039d9e06c8aecb9cef5e2e027907bbb321c1f83b71811ef3fdfc35",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-1002-2nd-saver": "15b60341f556b42f17b1ed2fb06257c964cd163d7b97a0582755ab39a436422a",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-1102-2nd-saver": "5c923f59bc8ff0b9542de443191b6cfc35a76096f56664b5252cae80c325a985",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-1202-2nd-saver": "82354a2be9d67661c41074a39b11005d1a033db35fb4a4c1ac4dfbfc4262e18a",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-1302-2nd-saver": "a8c22897ff2255892834a6886d21f728c795e31d12a0c448ef0d472228d4a1cc",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-1402-2nd-saver": "fba4d62e5d4253f9b22306d3c0fa7c59129a720038c0f77e5acb10d14c42fcc2",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-1502-2nd-saver": "4f793bacc056de1f6f3e737676577bd964ed0507bc5a596a7437c1b2d9fcca03",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-1602-2nd": "1c412ec081b48c1568ea1016ec57615adfece5dedc99d98c099d0d7ec6a29593",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-1702-2nd": "9b4b49eb26547d0e8884abfcace76800e4f1ffa583d8a9fd6585c88dcdf51d31",
  "DR-ZUE-BER-20261201-0902-2nd-saver|DR-BER-ZUE-20261202-1802-2nd": "5e6d1b26f986fa8705a935511768067b352d850b5e62e593c051bafa9517dad9",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-0702-2nd": "01da61b4e9a83aeb54240b6aef9540f0bc3fa9227423231ffc5dcd7cfaff0966",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-0802-2nd": "4fff7930b1b361bf350fe3f47d6d1fbf60192c928148b0df23cf3d73320b8889",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-0902-2nd-saver": "ea7317cd2d26d7a00a0a9fbd3b02321134a59da9a4d3ee4af70e1004943c5499",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-1002-2nd-saver": "b4bfacc4f5de3690ca43b00ae02a1edd8274192736cb8a67c1ff23357497292f",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-1102-2nd-saver": "f6f2d78f7d6fb74c532fe9c088978a37403e8380feafbdba125e3a9b2baee2f4",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-1202-2nd-saver": "9eb101799e696a2bd99329cc540f45def59b98928e36d6790bad8fb61a415b83",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-1302-2nd-saver": "7f432db4c8426f3c9091945a35db8668808113ccd1f95d4c6382d03fbe4c8c06",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-1402-2nd-saver": "edc9bb49c720310cae60c1b27f156a374b103f5da3377c7029fc78b900f7a561",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-1502-2nd-saver": "58ca3ee490b5c60cc0fef30da42ffa69ee8a6b0ec1f8323bd273e0fd96fc209f",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-1602-2nd": "8a7057f8efe0714c3abfcccd7d738ebcd7a275f78941948e6c7a278107017bb5",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-1702-2nd": "91089939ca47620dabf5f4efd33fa3accf6ad6f83383a9de2f47999b19c7bc53",
  "DR-ZUE-BER-20261201-1002-2nd-saver|DR-BER-ZUE-20261202-1802-2nd": "40a42d140fde600d9201b0e9731048a238931753fbb0cd8f3790d7950ed72cf0",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-0702-2nd": "584964c3592bd83939d8c35b4d0e3bc81b281fea538ca6fe269169e86cd21136",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-0802-2nd": "51a43b6a392ead83a7d02a6dfe5aa092c01beb82da30d67ff218d6a2aea9f81d",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-0902-2nd-saver": "6364e84c66139ac715eda7781f4d871e27fdb8a06139f4302ab65eee54b9f1dd",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-1002-2nd-saver": "64ac3b6acac04ea1e3512ca4001a511674eed22fbd6a20c43c64c2c8a177d6d7",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-1102-2nd-saver": "c2049cb7bd66197111f3add5cca0f803da33a2a9d9d6a9cb38852c4964ca374c",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-1202-2nd-saver": "59fac069565fb5b3078fd4f6d7f555aecd1afa3befbd1618a0f7e83cd9959546",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-1302-2nd-saver": "e7e8af058c521afeff8cf9b0db781123b88db670bf3ae57cae3bcc869f11954c",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-1402-2nd-saver": "fbc40b3cc3aca43b1010a0efad966e74e4b5096f952b6475c0f75bead0fc9be6",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-1502-2nd-saver": "1c6c7d51769e983f1692c70dfe6fa46661a3bdd17e69aba4d00cc649fc36d10b",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-1602-2nd": "11eaf770029403afe072c6b2c6f54254f018fb1416a0633eeeff64549eed25c2",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-1702-2nd": "90d90718100b92a3c24b02e64e7bcd7831c59a8e40deb212c948a44f5e2b3a58",
  "DR-ZUE-BER-20261201-1102-2nd-saver|DR-BER-ZUE-20261202-1802-2nd": "991810d6b8e53fa0c4d08c5f1f0269bf5d016e5797bb6035947772304fd2b093",
};

export const RATIONALE = "Cheapest 2nd-class fares in the time window on both legs.";
/** agents.state.SELECTED_RATIONALE: the rationale of a selection other than the recommended one. */
export const SELECTED_RATIONALE = "Selected by the approver from the offered trains.";

/**
 * What POST /runs/{run_id}/quote answers for a selection of `offer`, or null when the selection
 * is not one offered fare per leg (the API's 422).
 */
export function quoteFor(fareIds: string[], from: Offer = offer): Quote | null {
  if (fareIds.length !== from.legs.length) return null;
  const options: Option[] = [];
  for (const [i, leg] of from.legs.entries()) {
    const o = leg.options.find((x) => x.fare_id === fareIds[i]);
    if (!o) return null;
    options.push(o);
  }
  const hash = QUOTE_HASHES[fareIds.join("|")];
  if (!hash) return null;
  const recommended = from.legs.every((l, i) => l.recommended_fare_id === fareIds[i]);
  return {
    options,
    total_cents: options.reduce((n, o) => n + o.price_cents, 0) * from.passengers,
    currency: from.currency,
    rationale: recommended ? RATIONALE : SELECTED_RATIONALE,
    quote_hash: hash,
  };
}

export const RECOMMENDED_FARE_IDS = offer.legs.map((l) => l.recommended_fare_id);
export const QUOTE_HASH = QUOTE_HASHES[RECOMMENDED_FARE_IDS.join("|")];
/** The recommended quote: 09:02 saver both ways, CHF 56.00. */
export const quote: Quote = quoteFor(RECOMMENDED_FARE_IDS)!;

export const FLAGS = ["injection_flagged:travel_search.search_trains", "injection_flagged:travel_search.search_trains"];

/** Up to the approval pause: search both legs, then one fare lookup per train (6 out, 12 back). */
export const FARE_LOOKUPS = outboundOptions.length + returnOptions.length;
const PRICED_TRAJECTORY = [
  "supervisor",
  "planner",
  "travel_search.search_trains",
  "travel_search.search_trains",
  "supervisor",
  "pricing",
  ...Array<string>(FARE_LOOKUPS).fill("travel_search.get_fares"),
  "supervisor",
];

/** After an approval: hold the chosen seats (one per passenger per leg), then charge. */
const BOOKED_TRAJECTORY = [
  "approval",
  "supervisor",
  "booking_hold",
  "travel_search.hold_seat",
  "travel_search.hold_seat",
  "supervisor",
  "booking_pay",
  "payments.charge",
  "supervisor",
];

export const awaiting: RunView = {
  run_id: RUN_ID,
  status: "awaiting_approval",
  quote,
  interrupt: { quote, quote_hash: QUOTE_HASH, offer, approve_before: APPROVE_BEFORE },
  trajectory: PRICED_TRAJECTORY,
  payment: null,
  flags: FLAGS,
  // Demo payments are simulated: no processor ever reports on them.
  settlement: null,
};

export const completed: RunView = {
  ...awaiting,
  status: "completed",
  interrupt: null,
  trajectory: [...PRICED_TRAJECTORY, ...BOOKED_TRAJECTORY],
  payment: { payment_id: "PAY-4e97dfb3afd6", status: "captured", amount_cents: 5600, currency: "CHF" },
};

/** A completed run for another selection: the charge is that selection's server quote. */
export function completedFor(selected: Quote, base: RunView = completed): RunView {
  return {
    ...base,
    quote: selected,
    payment: base.payment ? { ...base.payment, amount_cents: selected.total_cents } : null,
  };
}

/** Rejected: nothing was held (holds only follow an approval) and nothing was charged. */
export const rejected: RunView = {
  ...awaiting,
  status: "rejected",
  interrupt: null,
  trajectory: [...PRICED_TRAJECTORY, "approval", "supervisor"],
};

const node = (n: string): RunEvent[] => [
  { type: "node_started", node: n },
  { type: "node_finished", node: n, interrupted: false },
];
const call = (agent: string, tool: string, flagged = false): RunEvent[] => [
  { type: "tool_call", agent, tool },
  { type: "tool_result", agent, tool, flagged },
];
const times = (n: number, events: RunEvent[]): RunEvent[] => Array.from({ length: n }, () => events).flat();

/** The create stream up to the priced offer (before the run pauses for approval). */
function pricedEvents(flagged: boolean): RunEvent[] {
  return [
    { type: "run_started", run_id: RUN_ID },
    ...node("supervisor"),
    { type: "node_started", node: "planner" },
    ...times(2, call("planner", "travel_search.search_trains", flagged)),
    { type: "node_finished", node: "planner", interrupted: false },
    ...node("supervisor"),
    { type: "node_started", node: "pricing" },
    ...times(FARE_LOOKUPS, call("pricing", "travel_search.get_fares")),
    { type: "node_finished", node: "pricing", interrupted: false },
    { type: "status", status: "awaiting_approval" },
  ];
}

function pauseEvents(view: RunView): RunEvent[] {
  return [
    ...node("supervisor"),
    { type: "node_started", node: "approval" },
    { type: "node_finished", node: "approval", interrupted: true },
    { type: "status", status: "awaiting_approval" },
    { type: "run_view", ...view },
  ];
}

export const createEvents: RunEvent[] = [...pricedEvents(true), ...pauseEvents(awaiting)];

/** The resumed stream after an approval: hold the chosen seats, then charge. */
export function approvedEvents(view: RunView): RunEvent[] {
  return [
    { type: "run_started", run_id: RUN_ID },
    { type: "node_started", node: "approval" },
    { type: "node_finished", node: "approval", interrupted: false },
    { type: "status", status: "running" },
    ...node("supervisor"),
    { type: "node_started", node: "booking_hold" },
    ...times(2, call("booking", "travel_search.hold_seat")),
    { type: "node_finished", node: "booking_hold", interrupted: false },
    ...node("supervisor"),
    { type: "node_started", node: "booking_pay" },
    ...call("booking", "payments.charge"),
    { type: "node_finished", node: "booking_pay", interrupted: false },
    { type: "status", status: "completed" },
    ...node("supervisor"),
    { type: "status", status: "completed" },
    { type: "run_view", ...view },
  ];
}

export const approveEvents: RunEvent[] = approvedEvents(completed);

export const rejectEvents: RunEvent[] = [
  { type: "run_started", run_id: RUN_ID },
  { type: "node_started", node: "approval" },
  { type: "node_finished", node: "approval", interrupted: false },
  { type: "status", status: "rejected" },
  ...node("supervisor"),
  { type: "status", status: "rejected" },
  { type: "run_view", ...rejected },
];

// ---- The same trip without the poisoned search result. ----

export const cleanAwaiting: RunView = { ...awaiting, flags: [] };
export const cleanCompleted: RunView = { ...completed, flags: [] };
export const cleanRejected: RunView = { ...rejected, flags: [] };
export const cleanCreateEvents: RunEvent[] = [...pricedEvents(false), ...pauseEvents(cleanAwaiting)];
export const cleanApproveEvents: RunEvent[] = approveEvents.map((e) =>
  e.type === "run_view" ? { type: "run_view", ...cleanCompleted } : e,
);
export const cleanRejectEvents: RunEvent[] = rejectEvents.map((e) =>
  e.type === "run_view" ? { type: "run_view", ...cleanRejected } : e,
);

// ---- Denied: the booking agent tries to charge right after pricing, before any approval. ----

export const DENY_REASON = "tool requires human approval";

export const refused: RunView = {
  run_id: RUN_ID,
  status: "refused",
  quote,
  interrupt: null,
  trajectory: [...PRICED_TRAJECTORY, "booking_pay", "supervisor"],
  payment: null,
  flags: ["policy_denied:payments.charge"],
  settlement: null,
};

export const deniedCreateEvents: RunEvent[] = [
  ...pricedEvents(false).filter((e) => !(e.type === "status")),
  ...node("supervisor"),
  { type: "node_started", node: "booking_pay" },
  { type: "policy_denied", agent: "booking", tool: "payments.charge", reason: DENY_REASON },
  { type: "node_finished", node: "booking_pay", interrupted: false },
  { type: "status", status: "refused" },
  ...node("supervisor"),
  { type: "status", status: "refused" },
  { type: "run_view", ...refused },
];

// ---- Error: the server fails mid-run (it sends the exception type only). ----
// The stream breaks while pricing's first get_fares call is in flight.

export const errorCreateEvents: RunEvent[] = [
  ...pricedEvents(false).slice(0, 13),
  { type: "error", error_type: "McpTransportError" },
];

// ---- Audit trails, as GET /audit?run_id=… returns them at each point of a run. ----

/** Renumbers entries into one contiguous chain (each prev_hash is the previous hash). */
function rechain(entries: AuditEntry[]): AuditEntry[] {
  return entries.map((e, i) => ({
    ...e,
    seq: i,
    prev_hash: i === 0 ? e.prev_hash : entries[i - 1].hash,
  }));
}

const APPROVAL_SEQ = poisonedAudit.findIndex((e) => e.event === "approval_decision");

export const audit = {
  poisoned: {
    awaiting: poisonedAudit.slice(0, APPROVAL_SEQ),
    completed: poisonedAudit,
    rejected: [
      ...poisonedAudit.slice(0, APPROVAL_SEQ),
      { ...poisonedAudit[APPROVAL_SEQ], data: { ...poisonedAudit[APPROVAL_SEQ].data, approved: false } },
    ],
  },
  clean: {
    awaiting: rechain(poisonedAudit.slice(0, APPROVAL_SEQ).filter((e) => e.event !== "injection_flagged")),
    completed: rechain(poisonedAudit.filter((e) => e.event !== "injection_flagged")),
    rejected: rechain([
      ...poisonedAudit.slice(0, APPROVAL_SEQ).filter((e) => e.event !== "injection_flagged"),
      { ...poisonedAudit[APPROVAL_SEQ], data: { ...poisonedAudit[APPROVAL_SEQ].data, approved: false } },
    ]),
  },
  denied: rechain([
    ...poisonedAudit.slice(0, APPROVAL_SEQ).filter((e) => e.event !== "injection_flagged"),
    {
      ...poisonedAudit[APPROVAL_SEQ + 1],
      event: "policy_denied",
      data: { tool: "payments.charge", reason: DENY_REASON, approved: false },
    },
  ]),
  error: rechain(poisonedAudit.slice(0, 5).filter((e) => e.event !== "injection_flagged")),
};

/** The events as an SSE body: one compact `data:` line per event plus a blank line. */
export function sseBody(events: RunEvent[]): string {
  return events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
}
