# Architecture — InfraMind

| | |
|---|---|
| **System** | InfraMind — multi-signal incident detection and graph-guided RCA on Kubernetes |
| **Status** | Target architecture (P0 scaffold; workers land P2–P8) |
| **Cluster** | `kind` only · SUT: Online Boutique · faults: Chaos Mesh |
| **Locked rules** | D1 deterministic RCA · D2 caller→callee · D9 LLM redaction |

InfraMind answers one question when a microservice fault occurs: **which service is the root
cause, with evidence**, delivered as **one deduplicated alert**. Root-cause **ranking is
deterministic**; the LLM **only summarises** the evidence bundle (D1). Long-lived workers
run on the test cluster; **Redis Streams** is the inter-stage conduit; **PostgreSQL** is the
source of truth.

| Document | Role |
|---|---|
| [`decision-tree.md`](decision-tree.md) | *Why* — locked decisions |
| [`surface-map.md`](surface-map.md) | *Wire shapes* — schemas, streams, API |
| [`standards/`](standards/README.md) | *How* — engineering conventions |

**Audience:** contributors, thesis §System Design, external reviewers.

---

## 1. System context (C4 — Level 1)

```mermaid
C4Context
    title InfraMind — system context

    Person(operator, "Operator", "Receives alerts, queries incidents")
    Person(engineer, "SRE / Developer", "Runs eval, injects faults")

    System(inframind, "InfraMind", "Ingests observability signals, detects incidents, ranks root causes, alerts once")

    System_Ext(k8s, "Kubernetes (kind)", "Hosts SUT + observability + InfraMind")
    System_Ext(obs, "Observability stack", "Prometheus, Loki, Jaeger/OTel, Alertmanager")
    System_Ext(llm_api, "LLM provider", "OpenAI / Anthropic / Ollama (summary only)")
    System_Ext(notify, "Notification sinks", "Slack, email, webhook")

    Rel(engineer, k8s, "Deploys, Chaos Mesh faults")
    Rel(k8s, obs, "Emits metrics, logs, traces, alerts")
    Rel(obs, inframind, "Pull + Alertmanager webhook")
    Rel(inframind, llm_api, "Evidence bundle in, summary out", "HTTPS")
    Rel(inframind, notify, "Deduped alert")
    Rel(inframind, operator, "Alert + GET /incidents")
    Rel(operator, inframind, "Read incident JSON")
```

---

## 2. Container view (C4 — Level 2)

```mermaid
C4Container
    title InfraMind — containers inside the cluster

    Person(operator, "Operator")

    Container_Boundary(im, "Namespace: inframind") {
        Container(ing, "Ingestion", "Python", "Collectors → Signal")
        Container(det, "Detection", "Python", "Z-score, EWMA, spikes")
        Container(cor, "Correlation", "Python", "Window, dedup, state machine")
        Container(rca, "RCA", "Python", "NetworkX graph, rank, evidence")
        Container(llm, "LLM explainer", "Python", "Prompt, validate, redact")
        Container(alt, "Alerting", "Python", "Route, dedup, payload")
        Container(api, "API", "FastAPI", "Webhooks, /incidents, health")
    }

    Container_Boundary(data, "Namespace: inframind-data") {
        ContainerDb(redis, "Redis Streams", "Bus", "stream:signals, stream:incidents")
        ContainerDb(pg, "PostgreSQL", "Store", "Incidents, audit log")
    }

    Container_Boundary(mon, "Namespace: monitoring") {
        ContainerDb(prom, "Prometheus", "Metrics")
        ContainerDb(loki, "Loki", "Logs")
        ContainerDb(jaeger, "Jaeger", "Traces")
        ContainerDb(am, "Alertmanager", "Alerts")
    }

    Rel(prom, ing, "PromQL pull")
    Rel(loki, ing, "LogQL pull")
    Rel(jaeger, ing, "Trace pull")
    Rel(am, api, "Webhook POST")

    Rel(ing, redis, "XADD signals")
    Rel(det, redis, "XREAD / XADD")
    Rel(cor, redis, "XREAD / XADD")
    Rel(rca, redis, "XREAD incidents")

    Rel(rca, llm, "Incident + candidates")
    Rel(llm, alt, "Summary + claims")
    Rel(rca, alt, "Ranked candidates")
    Rel(alt, pg, "INSERT")
    Rel(api, pg, "SELECT")
    Rel(alt, operator, "Notify")
    Rel(operator, api, "GET /incidents/{id}")
```

---

## 3. End-to-end data flow (one screen)

Solid lines: data path. Dashed: control or external push.

```mermaid
flowchart TB
    subgraph SOURCES["External observability"]
        direction LR
        PR(("Prometheus")):::source
        LO(("Loki")):::source
        JA(("Jaeger / OTel")):::source
        AM(("Alertmanager")):::source
    end

    subgraph BUS["Redis Streams — conduit only (D20)"]
        RS[("stream:signals")]:::bus
        RI[("stream:incidents")]:::bus
    end

    subgraph WORKERS["Pipeline workers"]
        direction TB
        ING["① Ingestion"]:::worker
        DC["② Detection + Correlation"]:::worker
        RCA["③ RCA<br/>graph + rank"]:::worker
        subgraph EXPLAIN["④ Explanation & delivery"]
            direction LR
            LLM["④a LLM<br/>summary only"]:::worker
            ALT["④b Alerting<br/>dedup + route"]:::worker
        end
    end

    subgraph PERSIST["Persistence & surface"]
        PG[("⑤ PostgreSQL<br/>source of truth")]:::store
        API["API · FastAPI"]:::api
    end

    OP(["Operator"]):::human

    PR -->|pull| ING
    LO -->|pull| ING
    JA -->|pull| ING
    AM -.->|webhook| API

    ING -->|Signal| RS
    RS --> DC
    DC -->|Anomaly groups| RI
    RI --> RCA
    RCA --> LLM
    RCA --> ALT
    LLM -->|validated summary| ALT
    ALT -->|INSERT| PG
    PG --> API
    ALT -->|one alert| OP
    API -->|read| OP

    classDef source fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a,stroke-width:2px
    classDef worker fill:#ffedd5,stroke:#c2410c,color:#7c2d12,stroke-width:2px
    classDef bus fill:#fef3c7,stroke:#b45309,color:#78350f,stroke-width:2px
    classDef store fill:#dcfce7,stroke:#15803d,color:#14532d,stroke-width:2px
    classDef api fill:#fef9c3,stroke:#a16207,color:#713f12,stroke-width:2px
    classDef human fill:#f3f4f6,stroke:#4b5563,color:#111827,stroke-width:2px
```

**Reading the diagram.** Collectors normalise backend payloads to `Signal`. Two stream
boundaries decouple workers: `stream:signals` after ingestion, `stream:incidents` after
correlation. RCA and downstream stages consume the incident stream; LLM and alerting share
the ranked result in-process before persistence.

---

## 4. Five-stage pipeline (proposal §3.2)

Aligned with the BSc proposal’s five layers; stage 4 splits into explainer + alerting.

```mermaid
flowchart LR
    subgraph S1["Stage 1"]
        A["Ingestion"]
    end
    subgraph S2["Stage 2"]
        B["Detection +<br/>Correlation"]
    end
    subgraph S3["Stage 3"]
        C["RCA"]
    end
    subgraph S4["Stage 4"]
        D["LLM explainer"]
        E["Alerting"]
    end
    subgraph S5["Stage 5"]
        F["Storage"]
    end
    G["API"]

    A -->|"Signal"| B
    B -->|"Incident<br/>(anomaly group)"| C
    C --> D
    C --> E
    D -->|"summary + claims"| E
    E -->|"INSERT"| F
    F --> G

    style S1 fill:#eff6ff,stroke:#3b82f6
    style S2 fill:#fff7ed,stroke:#f97316
    style S3 fill:#fdf4ff,stroke:#a855f7
    style S4 fill:#fefce8,stroke:#eab308
    style S5 fill:#ecfdf5,stroke:#10b981
```

| # | Stage | Package | Owner | Input | Output |
|---:|---|---|---|---|---|
| 1 | Ingestion | `ingestion/` | Moneem | PromQL, LogQL, traces, AM webhook | `Signal` → `stream:signals` |
| 2 | Detection + correlation | `detection/`, `correlation/` | Moneem | `Signal` | `Incident` / groups → `stream:incidents` |
| 3 | RCA | `rca/` **(core)** | Prome | Incidents + call graph | Ranked `Candidate[]` + `Evidence` |
| 4a | LLM explainer | `llm/` | Prome | Incident + candidates | `summary`, validated `claims[]` |
| 4b | Alerting | `alerting/` | Prome | Rank + summary | One deduped, routed alert |
| 5 | Storage | `storage/` | Prome | Rows | Postgres + audit log |
| — | API | `api/` | Prome | HTTP | Webhook ingress, `GET /incidents` |

**Pinned shapes:** [`surface-map.md`](surface-map.md) — `Signal`, `Anomaly`, `Incident`,
`Candidate`, `Evidence`, `Alert`.

**Adjacent (not runtime pipeline):**

| Area | Owner | Path |
|---|---|---|
| Testbed | Rifat | `testbed/` |
| Helm / `make up` | Rifat | `deploy/helm/inframind/` |
| Evaluation | Rifat | `evaluation/` |
| Agent workflow | team | `.claude/`, `docs/AGENT_WORKFLOW.md` |

---

## 5. Sequence — one fault, end to end

Typical path: redis unavailable; symptom at `frontend`; ~5 s to readable incident JSON.

```mermaid
sequenceDiagram
    autonumber
    box rgba(254,243,199,0.3) Evaluation
        actor Op as Operator
        participant CM as Chaos Mesh
    end
    box rgba(254,202,202,0.3) System under test
        participant SUT as Online Boutique
    end
    box rgba(219,234,254,0.3) Observability
        participant OBS as Prometheus / Loki / Jaeger
    end
    box rgba(255,237,213,0.3) InfraMind pipeline
        participant ING as Ingestion
        participant RS as Redis Streams
        participant DET as Detection
        participant COR as Correlation
        participant RCA as RCA engine
        participant LLM as LLM explainer
        participant ALT as Alerting
    end
    box rgba(220,252,231,0.3) Persistence
        participant PG as PostgreSQL
        participant API as API
    end

    Op->>CM: Apply PodChaos / network fault
    CM-->>SUT: Fault at t₀
    SUT-->>OBS: Metrics, logs, traces
    OBS-->>ING: Pull cycle
    ING->>RS: XADD stream:signals (Signal)
    RS-->>DET: XREAD
    Note over DET: Z-score fires (~t₀+1s)
    DET->>COR: Anomaly
    COR->>COR: Window + dedup
    COR->>RS: XADD stream:incidents
    RS-->>RCA: XREAD (~t₀+2s)
    RCA->>RCA: Walk toward callees (D2)
    RCA->>LLM: Incident + evidence bundle
    Note over LLM: Redact (D9); temperature=0
    LLM-->>RCA: Summary + claims (validated)
    RCA->>ALT: Rank + summary
    ALT->>ALT: Dedup, severity route
    ALT-->>Op: Single alert
    ALT->>PG: INSERT incident, evidence, audit
    Op->>API: GET /incidents/{id}
    API->>PG: SELECT
    PG-->>API: Row
    API-->>Op: 200 Incident JSON (~t₀+5s)
```

Timing varies with baseline warm-up, LLM retries, or dedup suppression; the **data flow**
does not change.

---

## 6. Logical layers

```mermaid
flowchart TB
    subgraph L3["Layer 3 — Public surface · api/"]
        direction LR
        EP1["POST webhooks/alertmanager"]
        EP2["GET /incidents · /healthz · /metrics"]
        EP3["Operators & integrations"]
    end

    subgraph L2["Layer 2 — Shared state"]
        direction LR
        REDIS["Redis Streams · conduit (D20)"]
        PG2["PostgreSQL · source of truth"]
    end

    subgraph L1["Layer 1 — Workers"]
        direction LR
        W_ING["ingestion"]
        W_DET["detection"]
        W_COR["correlation"]
        W_RCA["rca · llm · alerting"]
    end

    L1 -->|"XADD / XREAD / INSERT"| L2
    L2 -->|"SELECT / webhook"| L3

    style L1 fill:#ffedd5,stroke:#ea580c,color:#7c2d12
    style L2 fill:#fef3c7,stroke:#d97706,color:#78350f
    style L3 fill:#fef9c3,stroke:#ca8a04,color:#713f12
```

**Layer rules**

1. **No direct worker-to-worker calls across stage boundaries.** Consume the previous
   stage from Redis (or in-process only *after* the incident stream consumer, e.g. RCA → LLM).
2. **Publish at boundaries.** `stream:signals` after ingestion; `stream:incidents` after
   correlation.
3. **Bus ≠ truth (D20).** Postgres holds durable incidents; Redis may trim without losing
   audit data already inserted.
4. **HTTP ingress.** Alertmanager webhook → API; all other ingress is collector pull.

---

## 7. Kubernetes deployment (target — P8)

```mermaid
flowchart TB
    subgraph NS_CM["namespace: chaos-mesh"]
        CM["Chaos Mesh"]:::chaos
    end

    subgraph NS_DEF["namespace: default"]
        OB["Online Boutique + load"]:::sut
    end

    subgraph NS_MON["namespace: monitoring"]
        direction LR
        PROM["Prometheus"]:::mon
        LOKI["Loki"]:::mon
        JAE["Jaeger"]:::mon
        AM["Alertmanager"]:::mon
    end

    subgraph NS_PIPE["namespace: inframind — workers"]
        direction TB
        W1["Deployment: ingestion"]:::im
        W2["Deployment: detection + correlation"]:::im
        W3["Deployment: rca → llm → alerting"]:::im
    end

    subgraph NS_API["namespace: inframind — api"]
        WAPI["Deployment: api"]:::api
    end

    subgraph NS_DATA["namespace: inframind-data"]
        REDIS["Redis"]:::data
        POSTGRES["PostgreSQL"]:::data
    end

    CM -.->|fault injection| OB
    OB --> JAE
    PROM & LOKI & JAE -->|pull| W1
    AM -.->|webhook| WAPI

    W1 -->|XADD| REDIS
    W2 <-->|XREAD / XADD| REDIS
    W3 -->|XREAD| REDIS
    W3 -->|INSERT| POSTGRES
    WAPI -->|SELECT| POSTGRES

    classDef chaos fill:#fef9c3,stroke:#ca8a04,color:#713f12
    classDef sut fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
    classDef mon fill:#dbeafe,stroke:#2563eb,color:#1e40af
    classDef im fill:#ffedd5,stroke:#ea580c,color:#9a3412
    classDef api fill:#fef3c7,stroke:#ca8a04,color:#854d0e
    classDef data fill:#d1fae5,stroke:#059669,color:#065f46
```

| Edge style | Meaning |
|---|---|
| Solid | Data path (pull, stream, SQL) |
| Dashed | Control (Chaos injection, Alertmanager push) |

**RBAC:** InfraMind `ServiceAccount`s — read-only `get/list/watch` on pods, logs, events,
configmaps; no `exec`, no writes. Details: [`standards/11-helm-packaging.md`](standards/11-helm-packaging.md).

*Diagram is the P8 target; P0 runs unit tests locally and `docker compose` for Redis/Postgres only.*

---

## 8. RCA — graph walk and scoring

Deterministic rank (D1); edges **caller → callee**; walk from **symptom toward callees** (D2).

### 8.1 Example topology (Online Boutique)

```mermaid
flowchart LR
    FE["frontend<br/>(symptom)"]:::symptom
    CA["cart"]:::mid
    RE["redis<br/>(injected fault)"]:::root
    PC["productcatalogservice"]:::mid
    PA["payment"]:::mid
    PP["paymentprovider"]:::leaf
    SH["shipping"]:::mid
    SS["shippingservice"]:::leaf

    FE --> CA --> RE
    FE --> PA --> PP
    FE --> SH --> SS
    CA --> PC

    classDef symptom fill:#fecaca,stroke:#b91c1c,color:#7f1d1d,stroke-width:2px
    classDef root fill:#bbf7d0,stroke:#15803d,color:#14532d,stroke-width:3px
    classDef mid fill:#e0e7ff,stroke:#4338ca,color:#312e81
    classDef leaf fill:#f3f4f6,stroke:#9ca3af,color:#374151
```

```mermaid
flowchart TB
    START(["Incident opened<br/>symptom = frontend"]):::start
    W1["Traverse to callee: cart"]:::step
    W2["Traverse to callee: redis"]:::step
    BUNDLE["Bundle evidence<br/>(anomalies + snapshots + changes)"]:::step
    RANK["Score candidates<br/>(D15: evidence before rank)"]:::step
    OUT(["Top-1: redis"]):::out

    START --> W1 --> W2 --> BUNDLE --> RANK --> OUT

    classDef start fill:#dbeafe,stroke:#1d4ed8
    classDef step fill:#fff7ed,stroke:#ea580c
    classDef out fill:#dcfce7,stroke:#16a34a,stroke-width:2px
```

### 8.2 Scoring (default ranker)

```text
score(s) = w1 · anomaly_severity
         + w2 · earliest_onset
         + w3 · downstream_depth
         + w4 · change_correlation
```

**PageRank variant:** personalised PageRank on the call graph (α = 0.85), reported as
sensitivity analysis — not the production ranker (D7 baselines).

**Reproducibility:** `(graph, anomaly_set, seed)` fixes the rank; LLM uses `temperature=0`
and cache key `(scenario_id, prompt_version, evidence_bundle_hash, model)`.

Deep dive: [`standards/06-rca-scoring.md`](standards/06-rca-scoring.md),
[`standards/07-llm-explainer.md`](standards/07-llm-explainer.md).

---

## 9. Domain model (conceptual)

```mermaid
erDiagram
    SIGNAL ||--o{ ANOMALY : "triggers"
    ANOMALY }o--|| INCIDENT : "groups into"
    INCIDENT ||--|{ CANDIDATE : "ranks"
    CANDIDATE ||--|{ EVIDENCE : "bundles"
    INCIDENT ||--o{ ALERT : "emits"
    INCIDENT ||--|| INCIDENT_ROW : "persists as"

    SIGNAL {
        string id
        datetime ts
        string service
        string source
        string kind
    }
    INCIDENT {
        string id
        string state
        datetime opened_at
    }
    CANDIDATE {
        string service
        float score
    }
    EVIDENCE {
        string id
        string kind
    }
    ALERT {
        string fingerprint
        string severity
    }
```

---

## 10. Design rationale (summary)

| Choice | Rationale |
|---|---|
| Five stages, one owner each | Swap detectors or RCA rankers without cross-team rewrites; replay fixtures from stream 3 onward |
| Deterministic RCA (D1) | Same evidence → same rank; LLM version cannot change the primary |
| Redis + Postgres (D20) | Decouple workers; refuse silent loss if Postgres is down |
| Statistical detection (D3) | BSc scope: Z-score, EWMA, spikes — no DL training |
| LLM summariser only | Matches proposal §3.2.4 and literature on hallucination risk |

---

## 11. Cross-reference index

| Topic | Location |
|---|---|
| Why (D1, D2, D4, D6, D7, D9, D15, D20) | [`decision-tree.md`](decision-tree.md) |
| Schemas, env, API routes | [`surface-map.md`](surface-map.md) |
| Team, hard rules, agents | [`CLAUDE.md`](../CLAUDE.md) |
| Conventions 01–12 | [`standards/README.md`](standards/README.md) |
| Paper | `paper/` |

---

*Plain-text fallback:* if Mermaid does not render in your viewer, open this file on GitHub or
in VS Code / Cursor with Mermaid preview; the tables above remain the canonical stage contract.
