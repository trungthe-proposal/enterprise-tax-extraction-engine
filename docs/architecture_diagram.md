# Architecture Diagrams

> All diagrams are rendered natively by GitHub (Mermaid). The target system is
> intentionally shown as a generic **Target Portal**.

## 1. End-to-end data flow

```mermaid
flowchart TB
    subgraph UI["Operator Console (desktop UI)"]
        A1["Paste / import identifiers<br/>(spreadsheet clipboard)"]
        A2["Input sanitizer &<br/>format validator"]
        A3["Pre-flight checks<br/>(date range, filters, workload estimate)"]
        A4["Encrypted credential vault"]
    end

    subgraph ORCH["Orchestrator"]
        B1["Job planner<br/>(account x item x time window)"]
        B2[("Checkpoint store<br/>JSON, atomic writes")]
        B3["Isolation list<br/>(invalid / unknown inputs)"]
    end

    subgraph WORKER["Automation Worker (browser session)"]
        C1["Authenticate"]
        C2["Submit query"]
        C3["Local CAPTCHA OCR<br/>(ONNX, offline)"]
        C4{{"Outcome race<br/>result vs. network vs. notification"}}
        C5["Extract, unpack &<br/>post-process files"]
    end

    subgraph OBS["Interceptors"]
        D1["Network listener<br/>401 / 403 / 4xx / 5xx / transport"]
        D2["Notification watcher<br/>toast / alert / modal"]
    end

    subgraph OUT["Reporting"]
        E1["Structured logs<br/>(UI + rotating file)"]
        E2["Batch Execution Report"]
    end

    PORTAL[("Target Portal")]

    A1 --> A2 --> A3 --> B1
    A4 --> C1
    A2 -. "rejected items" .-> B3
    B1 --> B2
    B2 -- "pending items only" --> C1
    C1 --> C2
    C2 --> C3 --> C2
    C2 <--> PORTAL
    PORTAL -. "responses" .-> D1
    PORTAL -. "page state" .-> D2
    D1 --> C4
    D2 --> C4
    C2 --> C4

    C4 -- "ready" --> C5
    C4 -- "session expired" --> C1
    C4 -- "server error / notification / timeout" --> F1["Skip item &<br/>record reason"]
    C5 --> F2["Mark success"]
    F1 --> F3["Mark failed"]
    F2 --> B2
    F3 --> B2

    B2 -- "summary" --> E2
    B3 -- "isolated inputs" --> E2
    C4 --> E1
    E1 --> E2
```

## 2. Failure detection: event race instead of a fixed timeout

```mermaid
sequenceDiagram
    autonumber
    participant W as Worker
    participant L as Network listener
    participant N as Notification watcher
    participant P as Target Portal

    W->>L: mark() bookmark
    W->>P: submit query
    par observe in parallel
        P-->>L: HTTP response (200 / 401 / 403 / 5xx)
    and
        P-->>N: toast / alert / modal text
    end
    loop every ~250 ms
        W->>W: result ready?
        W->>L: any problem since mark?
        W->>N: any visible message?
    end
    alt result ready
        W->>W: extract and continue
    else 401 / 403
        W->>W: re-authenticate and retry the item
    else 5xx / notification
        W->>W: log verbatim message, skip item, move on (~1-2 s)
    else nothing at all
        W->>W: bounded timeout, then skip item
    end
```

## 3. Item lifecycle and resume

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> IN_PROGRESS: worker picks item
    IN_PROGRESS --> SUCCESS: data extracted
    IN_PROGRESS --> FAILED: error recorded with reason
    IN_PROGRESS --> PENDING: process crashed / power loss
    FAILED --> PENDING: operator fixes input and presses Resume
    SUCCESS --> [*]
```

**Resume rules**

| Item state at restart | Behaviour |
|---|---|
| `SUCCESS` | Never processed again |
| `FAILED` | Retried after the operator presses *Resume* |
| `IN_PROGRESS` | Treated as `PENDING` (it was interrupted) |
| `PENDING` | Processed normally |
| New items (input corrected) | Added as `PENDING`; existing progress is kept |
