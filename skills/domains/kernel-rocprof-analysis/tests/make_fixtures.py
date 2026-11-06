import csv
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
G = "Cijk_Ailk_Bljk_BBS_BH_MT128x128x32_MI32x32x8_ISA90a_WG128_2_1"
A = "void ck_tile::kentry<256, 2, ck_tile::FmhaFwdKernel<Problem>>(Kargs)"
T = "triton_poi_fused_add_mul_0"
N = "triton_red_fused_rms_norm_1"


def write(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def timeline(name, durations, base=1_000_000_000):
    d = root / name / "host-a" / "100"
    krows, arows, t, corr = [], [], base, 1
    stats = {}
    for kname, dur in durations:
        arows.append(
            ["HIP_RUNTIME_API_EXT", "hipExtModuleLaunchKernel", 100, 100, corr, t, t + 2_000]
        )
        krows.append(["KERNEL_DISPATCH", "Agent 1", 1, kname, corr, t + 3_000, t + 3_000 + dur])
        s = stats.setdefault(kname, [0, 0])
        s[0] += 1
        s[1] += dur
        t += 3_000 + dur + 7_000
        corr += 1
    write(
        d / "100_kernel_trace.csv",
        [
            "Kind",
            "Agent_Id",
            "Queue_Id",
            "Kernel_Name",
            "Correlation_Id",
            "Start_Timestamp",
            "End_Timestamp",
        ],
        krows,
    )
    write(
        d / "100_hip_api_trace.csv",
        [
            "Domain",
            "Function",
            "Process_Id",
            "Thread_Id",
            "Correlation_Id",
            "Start_Timestamp",
            "End_Timestamp",
        ],
        arows,
    )
    write(
        d / "100_memory_copy_trace.csv",
        [
            "Kind",
            "Direction",
            "Stream_Id",
            "Source_Agent_Id",
            "Destination_Agent_Id",
            "Correlation_Id",
            "Start_Timestamp",
            "End_Timestamp",
        ],
        [
            [
                "MEMORY_COPY",
                "MEMORY_COPY_HOST_TO_DEVICE",
                0,
                "Agent 0",
                "Agent 1",
                0,
                base - 500_000,
                base - 400_000,
            ]
        ],
    )
    srows = [
        [k, c, tot, f"{tot / c:.4f}", "0.00", tot // c, tot // c, "0.0"]
        for k, (c, tot) in stats.items()
    ]
    write(
        d / "100_kernel_stats.csv",
        ["Name", "Calls", "TotalDurationNs", "AverageNs", "Percentage", "MinNs", "MaxNs", "StdDev"],
        srows,
    )
    (root / name / "capture.json").write_text(
        json.dumps({"schema": 1, "recipe": "timeline", "name": name}, indent=2) + "\n"
    )


timeline(
    "timeline_a", [(G, 400_000), (A, 250_000), (T, 20_000), (G, 400_000), (A, 250_000), (T, 20_000)]
)
timeline(
    "timeline_b", [(G, 300_000), (A, 250_000), (N, 15_000), (G, 300_000), (A, 250_000), (N, 15_000)]
)

hdr = [
    "Correlation_Id",
    "Dispatch_Id",
    "Agent_Id",
    "Queue_Id",
    "Process_Id",
    "Thread_Id",
    "Grid_Size",
    "Kernel_Id",
    "Kernel_Name",
    "Workgroup_Size",
    "LDS_Block_Size",
    "Scratch_Size",
    "VGPR_Count",
    "Accum_VGPR_Count",
    "SGPR_Count",
    "Counter_Name",
    "Counter_Value",
    "Start_Timestamp",
    "End_Timestamp",
]


def pmc(name, values):
    rows = []
    for disp, (kname, start) in enumerate([(G, 2_000_000), (A, 3_000_000)], 1):
        for counter, value in values[kname].items():
            rows.append(
                [
                    disp,
                    disp,
                    1,
                    1,
                    200,
                    200,
                    131072,
                    disp,
                    kname,
                    256,
                    0,
                    0,
                    128,
                    0,
                    80,
                    counter,
                    value,
                    start,
                    start + 400_000,
                ]
            )
    write(root / name / "host-a" / "200_counter_collection.csv", hdr, rows)


pmc(
    "pmc_a",
    {
        G: {
            "SQ_WAVES": 2048.0,
            "GRBM_GUI_ACTIVE": 600000.0,
            "TCC_HIT_sum": 9000.0,
            "TCC_MISS_sum": 1000.0,
        },
        A: {
            "SQ_WAVES": 1024.0,
            "GRBM_GUI_ACTIVE": 600000.0,
            "TCC_HIT_sum": 5000.0,
            "TCC_MISS_sum": 5000.0,
        },
    },
)
pmc(
    "pmc_b",
    {
        G: {
            "SQ_WAVES": 2048.0,
            "GRBM_GUI_ACTIVE": 600000.0,
            "TCC_HIT_sum": 9500.0,
            "TCC_MISS_sum": 500.0,
        },
        A: {
            "SQ_WAVES": 1024.0,
            "GRBM_GUI_ACTIVE": 600000.0,
            "TCC_HIT_sum": 5000.0,
            "TCC_MISS_sum": 5000.0,
        },
    },
)

att = root / "att" / "ui_output_agent_1_dispatch_1"
att.mkdir(parents=True, exist_ok=True)
code = {
    "header": "ISA, _, LineNumber, Source, Codeobj, Vaddr, Hit, Latency, Stall, Idle",
    "code": [
        ["s_load_dwordx4 s[4:7], s[0:1], 0x0", "", 1, "attn.py:12", "", 4096, 50, 2000, 100, 2000],
        [
            "v_mfma_f32_16x16x16_f16 a[0:3], v[0:1], v[2:3], a[0:3]",
            "",
            2,
            "attn.py:40",
            "",
            4104,
            200,
            5000,
            500,
            4500,
        ],
        [
            "buffer_load_dwordx4 v[4:7], v0, s[8:11], 0 offen",
            "",
            3,
            "attn.py:38",
            "",
            4108,
            200,
            9000,
            8500,
            500,
        ],
        ["s_waitcnt vmcnt(0)", "", 4, "attn.py:39", "", 4112, 200, 8600, 3000, 200],
        ["s_endpgm", "", 0, "", "", 4120, 1, 10, 0, 1],
    ],
}
(att / "code.json").write_text(json.dumps(code, indent=1) + "\n")

events = [
    {
        "ph": "X",
        "cat": "user_annotation",
        "name": "ProfilerStep#1",
        "pid": 1,
        "tid": 1,
        "ts": 0,
        "dur": 10_000,
        "args": {},
    }
]
for i in range(6):
    ts = 100 + i * 1000
    eid = i + 1
    corr = 5000 + i
    events.append(
        {
            "ph": "X",
            "cat": "cpu_op",
            "name": "aten::addmm",
            "pid": 1,
            "tid": 1,
            "ts": ts,
            "dur": 200,
            "args": {
                "External id": eid,
                "Input Dims": [[11008], [32, 4096], [4096, 11008]],
                "Input type": ["c10::BFloat16"] * 3,
            },
        }
    )
    events.append(
        {
            "ph": "X",
            "cat": "hip_runtime",
            "name": "hipLaunchKernel",
            "pid": 1,
            "tid": 1,
            "ts": ts + 5,
            "dur": 2,
            "args": {"External id": eid, "correlation": corr},
        }
    )
    events.append(
        {
            "ph": "X",
            "cat": "kernel",
            "name": "Cijk_Ailk_Bljk_HHS_BH",
            "pid": 0,
            "tid": 2,
            "ts": ts + 10,
            "dur": 300,
            "args": {"correlation": corr, "device": 0},
        }
    )
torch_dir = root / "torch"
torch_dir.mkdir(parents=True, exist_ok=True)
trace = {
    "schemaVersion": 1,
    "deviceProperties": [{"id": 0, "name": "AMD Instinct MI210"}],
    "traceEvents": events,
}
(torch_dir / "trace.pt.trace.json").write_text(json.dumps(trace) + "\n")
