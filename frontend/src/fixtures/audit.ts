// Audit entries of a poisoned round trip in the shape GET /audit returns for the mock API
// (LLM_PROVIDER=mock, TRAVEL_SEARCH_POISONED=1), trimmed to the fields the UI reads: no args, no
// results. Graph order: planner, pricing (18 fare lookups: 6 trains out, 12 back), the human
// approval, then the seat holds for the chosen fares and the charge. Guard findings are shown as the guard words them (path:
// pattern), from controls/injection_guard.py. Hashes are chained like controls/audit_log.py.
import type { AuditEntry } from "../types";

export const poisonedAudit: AuditEntry[] = [
  {
    "seq": 0,
    "ts": "2026-11-21T16:18:24.796863+00:00",
    "run_id": "demo-run",
    "actor": "supervisor",
    "event": "intent",
    "data": {
      "kind": "book_trip"
    },
    "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
    "hash": "7d2c25f87e0f0f72f0f1a382ed713fb01d10919b267232fdbd9d08ce5bfbc25c"
  },
  {
    "seq": 1,
    "ts": "2026-11-21T16:18:24.799863+00:00",
    "run_id": "demo-run",
    "actor": "planner",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.search_trains"
    },
    "prev_hash": "7d2c25f87e0f0f72f0f1a382ed713fb01d10919b267232fdbd9d08ce5bfbc25c",
    "hash": "ada1af244528733a77163f79ed24541e5fbc0949e6db38beedd775d71db9afe2"
  },
  {
    "seq": 2,
    "ts": "2026-11-21T16:18:24.802863+00:00",
    "run_id": "demo-run",
    "actor": "planner",
    "event": "tool_result",
    "data": {
      "flagged": true,
      "tool": "travel_search.search_trains"
    },
    "prev_hash": "ada1af244528733a77163f79ed24541e5fbc0949e6db38beedd775d71db9afe2",
    "hash": "bafde26f43e465d04754d112a19783bd5c35ddd96fa1a0bd1c39da894244947f"
  },
  {
    "seq": 3,
    "ts": "2026-11-21T16:18:24.805863+00:00",
    "run_id": "demo-run",
    "actor": "planner",
    "event": "injection_flagged",
    "data": {
      "tool": "travel_search.search_trains",
      "findings": [
        "[0].notes: ignore (all )?(previous|prior) instructions"
      ]
    },
    "prev_hash": "bafde26f43e465d04754d112a19783bd5c35ddd96fa1a0bd1c39da894244947f",
    "hash": "acedf0ef5b21519874cdacb58564e1272d40301f9f34328020d3c22795e0e0f7"
  },
  {
    "seq": 4,
    "ts": "2026-11-21T16:18:24.808863+00:00",
    "run_id": "demo-run",
    "actor": "planner",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.search_trains"
    },
    "prev_hash": "acedf0ef5b21519874cdacb58564e1272d40301f9f34328020d3c22795e0e0f7",
    "hash": "c5101beabb9da51e67931ba67a6d6d90b48d7bfc55bd6e65692f6a6f50a69ae2"
  },
  {
    "seq": 5,
    "ts": "2026-11-21T16:18:24.811863+00:00",
    "run_id": "demo-run",
    "actor": "planner",
    "event": "tool_result",
    "data": {
      "flagged": true,
      "tool": "travel_search.search_trains"
    },
    "prev_hash": "c5101beabb9da51e67931ba67a6d6d90b48d7bfc55bd6e65692f6a6f50a69ae2",
    "hash": "f0e36e22366f3de8f63ccde5080ea6408a681794e34c1e9f7ba881e3d419a190"
  },
  {
    "seq": 6,
    "ts": "2026-11-21T16:18:24.814863+00:00",
    "run_id": "demo-run",
    "actor": "planner",
    "event": "injection_flagged",
    "data": {
      "tool": "travel_search.search_trains",
      "findings": [
        "[0].notes: ignore (all )?(previous|prior) instructions"
      ]
    },
    "prev_hash": "f0e36e22366f3de8f63ccde5080ea6408a681794e34c1e9f7ba881e3d419a190",
    "hash": "77d525f7cd1de8d2ee9722bc2b881e5ba8ba6e4cc0361272d04b39d79aa24e82"
  },
  {
    "seq": 7,
    "ts": "2026-11-21T16:18:24.817863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "77d525f7cd1de8d2ee9722bc2b881e5ba8ba6e4cc0361272d04b39d79aa24e82",
    "hash": "8800a9ab76eeb5c79e901a11c6d581eade6bef5d4234150a6eece36e1b8ee744"
  },
  {
    "seq": 8,
    "ts": "2026-11-21T16:18:24.820863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "8800a9ab76eeb5c79e901a11c6d581eade6bef5d4234150a6eece36e1b8ee744",
    "hash": "331793d505f853e61f1c02b3f34ac6ae10be732c1c4d482bf3948010ff3b102a"
  },
  {
    "seq": 9,
    "ts": "2026-11-21T16:18:24.823863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "331793d505f853e61f1c02b3f34ac6ae10be732c1c4d482bf3948010ff3b102a",
    "hash": "962a9620b4962075afb22867eeeaa232b36a06cd6cfaba0c4ff278eb283135e7"
  },
  {
    "seq": 10,
    "ts": "2026-11-21T16:18:24.826863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "962a9620b4962075afb22867eeeaa232b36a06cd6cfaba0c4ff278eb283135e7",
    "hash": "b5590ffd0e21ddaa4d505ba2c695f9e2401e4fa7f81118ee4d6d632afd60f6e5"
  },
  {
    "seq": 11,
    "ts": "2026-11-21T16:18:24.829863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "b5590ffd0e21ddaa4d505ba2c695f9e2401e4fa7f81118ee4d6d632afd60f6e5",
    "hash": "eed08da096e191ad3256b6a99c49d9c898fcf255cbc0e0b774ba31d19c33d137"
  },
  {
    "seq": 12,
    "ts": "2026-11-21T16:18:24.832863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "eed08da096e191ad3256b6a99c49d9c898fcf255cbc0e0b774ba31d19c33d137",
    "hash": "2a6e7672224357bda9ad05f361ea61663b98b0bfb02636941678124bae8d03df"
  },
  {
    "seq": 13,
    "ts": "2026-11-21T16:18:24.835863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "2a6e7672224357bda9ad05f361ea61663b98b0bfb02636941678124bae8d03df",
    "hash": "5596cc23b3879f72f499da787ed45a5044bbfbf101ebf10a19643205e3c4d1e0"
  },
  {
    "seq": 14,
    "ts": "2026-11-21T16:18:24.838863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "5596cc23b3879f72f499da787ed45a5044bbfbf101ebf10a19643205e3c4d1e0",
    "hash": "58d411fef2f5eeebdda8de9d539dc0e95abb77a41c7faaecd18df1583a2a10c7"
  },
  {
    "seq": 15,
    "ts": "2026-11-21T16:18:24.841863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "58d411fef2f5eeebdda8de9d539dc0e95abb77a41c7faaecd18df1583a2a10c7",
    "hash": "24cce448a7122eea6a5c6d3dbd1c87d2e682cd9778a74933f16233acd1a79dda"
  },
  {
    "seq": 16,
    "ts": "2026-11-21T16:18:24.844863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "24cce448a7122eea6a5c6d3dbd1c87d2e682cd9778a74933f16233acd1a79dda",
    "hash": "b8fe5fab23b1cfb62b4d99e28329765b343ee20fe5815d961e1c9053e1085ddc"
  },
  {
    "seq": 17,
    "ts": "2026-11-21T16:18:24.847863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "b8fe5fab23b1cfb62b4d99e28329765b343ee20fe5815d961e1c9053e1085ddc",
    "hash": "b7005640d527792f189a9d5b32988651ac8b280b2b16d41a4649e5d9c666da1b"
  },
  {
    "seq": 18,
    "ts": "2026-11-21T16:18:24.850863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "b7005640d527792f189a9d5b32988651ac8b280b2b16d41a4649e5d9c666da1b",
    "hash": "d68cd6c62bd39e817d6395e907c3466aa3a2b8ed406b1c057b759479e3962071"
  },
  {
    "seq": 19,
    "ts": "2026-11-21T16:18:24.853863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "d68cd6c62bd39e817d6395e907c3466aa3a2b8ed406b1c057b759479e3962071",
    "hash": "c2d35b089babbf413e1132ca58c7f80515fc7611293549ee462ab8149f974196"
  },
  {
    "seq": 20,
    "ts": "2026-11-21T16:18:24.856863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "c2d35b089babbf413e1132ca58c7f80515fc7611293549ee462ab8149f974196",
    "hash": "e06ce7e140df197fb317566366ab37a61aa3601662764922e1ca8ee264a7e694"
  },
  {
    "seq": 21,
    "ts": "2026-11-21T16:18:24.859863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "e06ce7e140df197fb317566366ab37a61aa3601662764922e1ca8ee264a7e694",
    "hash": "8d76fb746b0968a7956252bb45685b2e8a01827eff13434a74c45a9f6b8642c6"
  },
  {
    "seq": 22,
    "ts": "2026-11-21T16:18:24.862863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "8d76fb746b0968a7956252bb45685b2e8a01827eff13434a74c45a9f6b8642c6",
    "hash": "06589194f99ab8246e54a9b2f97bae36eb2dd024aec001ba50b79b3ca86637b0"
  },
  {
    "seq": 23,
    "ts": "2026-11-21T16:18:24.865863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "06589194f99ab8246e54a9b2f97bae36eb2dd024aec001ba50b79b3ca86637b0",
    "hash": "261ac1a6f35dd9a4fc013a9c151cc40eb926e28968d089428ede1efd2533a5f8"
  },
  {
    "seq": 24,
    "ts": "2026-11-21T16:18:24.868863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "261ac1a6f35dd9a4fc013a9c151cc40eb926e28968d089428ede1efd2533a5f8",
    "hash": "e4285e7e850081e981d0fd03f653b45b62f335d7d5b0f8f149928d6816ecb39e"
  },
  {
    "seq": 25,
    "ts": "2026-11-21T16:18:24.871863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "e4285e7e850081e981d0fd03f653b45b62f335d7d5b0f8f149928d6816ecb39e",
    "hash": "b3b123cb653c14d5189267631a1785ee16819752db3d88af9631c3238c9a0cb8"
  },
  {
    "seq": 26,
    "ts": "2026-11-21T16:18:24.874863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "b3b123cb653c14d5189267631a1785ee16819752db3d88af9631c3238c9a0cb8",
    "hash": "6d557c642ae3692b464b6806ec24fd053797f4ed6838a90421ec26274d40f0b4"
  },
  {
    "seq": 27,
    "ts": "2026-11-21T16:18:24.877863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "6d557c642ae3692b464b6806ec24fd053797f4ed6838a90421ec26274d40f0b4",
    "hash": "094fffe0895cd6978f903234c8ae1b936d1576a52f9c3377470328a57b76da94"
  },
  {
    "seq": 28,
    "ts": "2026-11-21T16:18:24.880863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "094fffe0895cd6978f903234c8ae1b936d1576a52f9c3377470328a57b76da94",
    "hash": "6a476e243517750e1abf410354f8a0a63e2b00a99736628deee4718b10b106b5"
  },
  {
    "seq": 29,
    "ts": "2026-11-21T16:18:24.883863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "6a476e243517750e1abf410354f8a0a63e2b00a99736628deee4718b10b106b5",
    "hash": "62a9ff12aefe668e7e520cc3692b200111cd20f98ed1ecff85fe0f8da4146f65"
  },
  {
    "seq": 30,
    "ts": "2026-11-21T16:18:24.886863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "62a9ff12aefe668e7e520cc3692b200111cd20f98ed1ecff85fe0f8da4146f65",
    "hash": "071227af5452ac523a35fd3d98f5d15a08cdc353f8400f4d4b0c066c44cf00c1"
  },
  {
    "seq": 31,
    "ts": "2026-11-21T16:18:24.889863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "071227af5452ac523a35fd3d98f5d15a08cdc353f8400f4d4b0c066c44cf00c1",
    "hash": "262d0223ea0f427fdb390c22d1b9d7418266f84d470a79001b5988efd3878c36"
  },
  {
    "seq": 32,
    "ts": "2026-11-21T16:18:24.892863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "262d0223ea0f427fdb390c22d1b9d7418266f84d470a79001b5988efd3878c36",
    "hash": "8b630273b51a812f4b259e0289ca8928178fc4eced96fdabfd336fe57ecc6de7"
  },
  {
    "seq": 33,
    "ts": "2026-11-21T16:18:24.895863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "8b630273b51a812f4b259e0289ca8928178fc4eced96fdabfd336fe57ecc6de7",
    "hash": "505e54c24a8001f9f420d397861fa08bd8b7e195da0f97f4274d442098a0340f"
  },
  {
    "seq": 34,
    "ts": "2026-11-21T16:18:24.898863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "505e54c24a8001f9f420d397861fa08bd8b7e195da0f97f4274d442098a0340f",
    "hash": "17d7536b1853d77dc28106b7420453c4ccae4b66f638366609f08b8dfa4ef111"
  },
  {
    "seq": 35,
    "ts": "2026-11-21T16:18:24.901863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "17d7536b1853d77dc28106b7420453c4ccae4b66f638366609f08b8dfa4ef111",
    "hash": "8f6c552fca07cf7c7e9ba3bf2d810c187e8143e182b1e23d390c12dc3a560da4"
  },
  {
    "seq": 36,
    "ts": "2026-11-21T16:18:24.904863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "8f6c552fca07cf7c7e9ba3bf2d810c187e8143e182b1e23d390c12dc3a560da4",
    "hash": "6a9dd5ba0f86d869fcff0fc173730dbad9efb7ad0c5f60fc8cc759b4f409d590"
  },
  {
    "seq": 37,
    "ts": "2026-11-21T16:18:24.907863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "6a9dd5ba0f86d869fcff0fc173730dbad9efb7ad0c5f60fc8cc759b4f409d590",
    "hash": "11a3a68c1d8de44a6b82c8f571f0b589c240da11d05fb025314c30c80d9dbb94"
  },
  {
    "seq": 38,
    "ts": "2026-11-21T16:18:24.910863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "11a3a68c1d8de44a6b82c8f571f0b589c240da11d05fb025314c30c80d9dbb94",
    "hash": "fe26aef71bc89ba5c80c09f7f832c8c9f9f71161336c837f24d81f8a9f2bf3e7"
  },
  {
    "seq": 39,
    "ts": "2026-11-21T16:18:24.913863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "fe26aef71bc89ba5c80c09f7f832c8c9f9f71161336c837f24d81f8a9f2bf3e7",
    "hash": "85d811a66758726472e80f7e7fd59a1187ccb06096e0d35ae90cfb7e4eb0fd75"
  },
  {
    "seq": 40,
    "ts": "2026-11-21T16:18:24.916863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "85d811a66758726472e80f7e7fd59a1187ccb06096e0d35ae90cfb7e4eb0fd75",
    "hash": "da5b99aaedd9903ecef5422e9db696f7e59143be084a1dfcf89ca0418e643010"
  },
  {
    "seq": 41,
    "ts": "2026-11-21T16:18:24.919863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "da5b99aaedd9903ecef5422e9db696f7e59143be084a1dfcf89ca0418e643010",
    "hash": "e59ec103410176e0c397300272317758b5d67f0f39b68ee4fc62957841d3d451"
  },
  {
    "seq": 42,
    "ts": "2026-11-21T16:18:24.922863+00:00",
    "run_id": "demo-run",
    "actor": "pricing",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.get_fares"
    },
    "prev_hash": "e59ec103410176e0c397300272317758b5d67f0f39b68ee4fc62957841d3d451",
    "hash": "2035b3446f3140facefa323918fdd716c38369f97ed7759c24b8190f1b9f02d1"
  },
  {
    "seq": 43,
    "ts": "2026-11-21T16:18:31.399863+00:00",
    "run_id": "demo-run",
    "actor": "human",
    "event": "approval_decision",
    "data": {
      "approved": true,
      "approver": "demo-approver",
      "quote_hash": "39c313e3ba039d9e06c8aecb9cef5e2e027907bbb321c1f83b71811ef3fdfc35",
      "fare_ids": [
        "DR-ZUE-BER-20261201-0902-2nd-saver",
        "DR-BER-ZUE-20261202-0902-2nd-saver"
      ]
    },
    "prev_hash": "2035b3446f3140facefa323918fdd716c38369f97ed7759c24b8190f1b9f02d1",
    "hash": "e9bd9a80af3bfa770dec7bf15bcfe6041101d8383bf18d380a10048706a1985f"
  },
  {
    "seq": 44,
    "ts": "2026-11-21T16:18:31.402863+00:00",
    "run_id": "demo-run",
    "actor": "booking",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.hold_seat"
    },
    "prev_hash": "e9bd9a80af3bfa770dec7bf15bcfe6041101d8383bf18d380a10048706a1985f",
    "hash": "2b8d66e471aaa11b45a3e6061f24a0b5e365cd69f893262640659853676f3b3b"
  },
  {
    "seq": 45,
    "ts": "2026-11-21T16:18:31.405863+00:00",
    "run_id": "demo-run",
    "actor": "booking",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.hold_seat"
    },
    "prev_hash": "2b8d66e471aaa11b45a3e6061f24a0b5e365cd69f893262640659853676f3b3b",
    "hash": "86e369d29f2ec65edfdb10f298e65b1e548b146573205abb1a4c123539dda226"
  },
  {
    "seq": 46,
    "ts": "2026-11-21T16:18:31.408863+00:00",
    "run_id": "demo-run",
    "actor": "booking",
    "event": "tool_call",
    "data": {
      "approved": false,
      "tool": "travel_search.hold_seat"
    },
    "prev_hash": "86e369d29f2ec65edfdb10f298e65b1e548b146573205abb1a4c123539dda226",
    "hash": "0e7934a88c0a162ec319b65cce3ae3e427b9ee237bc947a3c509aeb3dddb8eed"
  },
  {
    "seq": 47,
    "ts": "2026-11-21T16:18:31.411863+00:00",
    "run_id": "demo-run",
    "actor": "booking",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "travel_search.hold_seat"
    },
    "prev_hash": "0e7934a88c0a162ec319b65cce3ae3e427b9ee237bc947a3c509aeb3dddb8eed",
    "hash": "bd3003554ce996cb054409c2b717952711402264e7b80ba746229dfc90ef594b"
  },
  {
    "seq": 48,
    "ts": "2026-11-21T16:18:31.414863+00:00",
    "run_id": "demo-run",
    "actor": "booking",
    "event": "tool_call",
    "data": {
      "approved": true,
      "tool": "payments.charge"
    },
    "prev_hash": "bd3003554ce996cb054409c2b717952711402264e7b80ba746229dfc90ef594b",
    "hash": "b4b28f580d137f63e3d96082ae4c365e574717dc0ac91ea63f48859b9cb355a9"
  },
  {
    "seq": 49,
    "ts": "2026-11-21T16:18:31.417863+00:00",
    "run_id": "demo-run",
    "actor": "booking",
    "event": "tool_result",
    "data": {
      "flagged": false,
      "tool": "payments.charge"
    },
    "prev_hash": "b4b28f580d137f63e3d96082ae4c365e574717dc0ac91ea63f48859b9cb355a9",
    "hash": "2054463a128ba2738c509228eb5e1e0042717066e34e1243aaf08c48dd89e4d1"
  }
];
