# Codebase UML

This document summarizes the current patent translation codebase using Mermaid
UML-style diagrams.

## High-Level Component Diagram

```mermaid
flowchart LR
    CLI[main.py] --> Translator[PatentTranslator]
    Batch[batch.py] --> Translator
    Download[download.py] --> S3[s3.py]

    Translator --> Config[ClientConfig]
    Translator --> Client[LLMClient]
    Translator --> Graph[LangGraph build_graph]

    Graph --> Load[load]
    Graph --> Classify[classify]
    Graph --> Static[apply_static]
    Graph --> BodyChunk[chunk_body]
    Graph --> BodyTranslate[translate_body]
    Graph --> BodyReview[review body]
    Graph --> AbstractChunk[chunk_abstract]
    Graph --> AbstractTranslate[translate_abstract]
    Graph --> AbstractReview[review abstract]
    Graph --> ClaimChunk[chunk_claims]
    Graph --> ClaimTranslate[translate_claims]
    Graph --> ClaimReview[review claims]
    Graph --> Write[write]

    Client --> OpenAI[OpenAI-compatible API]
    BodyTranslate --> AgentPrompts[agent/prompts.py]
    AbstractTranslate --> AgentPrompts
    ClaimTranslate --> AgentPrompts
    ClaimTranslate --> ClaimPlanning[claim_planning.py]
    BodyReview --> AgentPrompts
    AbstractReview --> AgentPrompts
    ClaimReview --> AgentPrompts

    Classify --> DocxUtils[docx_utils.py]
    ClaimChunk --> DocxUtils
    Write --> DocxUtils
    ClaimTranslate --> DocxUtils
    BodyTranslate --> Glossary[glossary.py]
    AbstractTranslate --> Glossary
    ClaimTranslate --> Glossary
```

## Translation Pipeline

```mermaid
flowchart TD
    Start([translate_document]) --> Load[load DOCX]
    Load --> Classify[classify paragraphs]
    Classify --> Static[apply static transforms]

    Static --> ChunkBody[chunk_body]
    ChunkBody --> TranslateBody[translate_body]
    TranslateBody --> ReviewBody{review body?}
    ReviewBody -->|revise| ReviseBody[review_revise_body]
    ReviewBody -->|skip| ChunkAbstract[chunk_abstract]
    ReviseBody --> ChunkAbstract

    ChunkAbstract --> TranslateAbstract[translate_abstract]
    TranslateAbstract --> ReviewAbstract{review abstract?}
    ReviewAbstract -->|revise| ReviseAbstract[review_revise_abstract]
    ReviewAbstract -->|skip| ChunkClaims[chunk_claims]
    ReviseAbstract --> ChunkClaims

    ChunkClaims --> TranslateClaims[translate_claims]
    TranslateClaims --> ReviewClaims{review claims?}
    ReviewClaims -->|revise| ReviseClaims[review_revise_claims]
    ReviewClaims -->|skip| Write[write DOCX]
    ReviseClaims --> Write
    Write --> End([output DOCX])
```

## Core Class Diagram

```mermaid
classDiagram
    class PatentTranslator {
        +ClientConfig config
        +LLMClient client
        -_graph
        +translate_document(input_path, output_path, font, delay, verbose, review, progress_callback) None
    }

    class ClientConfig {
        +str model
        +str base_url
        +str api_key
        +float temperature
        +int max_tokens
        +dict extra_params
        +from_env() ClientConfig
    }

    class LLMClient {
        +ClientConfig config
        -OpenAI _client
        +complete(messages) str
        +stream(messages) Iterator~str~
    }

    class TranslationState {
        <<TypedDict>>
        +Path input_path
        +Path output_path
        +str font
        +bool review
        +bool verbose
        +float delay
        +Callable progress
        +LLMClient client
        +Document doc
        +list~ParagraphRecord~ records
        +list~Chunk~ chunks_body
        +list~Chunk~ chunks_abstract
        +list~Chunk~ chunks_claims
        +dict~int, ClaimPlan~ claim_plans
        +dict~str, str~ glossary
        +float started_at
    }

    class ParagraphRecord {
        +int index
        +ParagraphKind kind
        +Any para
        +str raw
        +str section
        +str mapped
        +int claim_num
        +bool mixed
    }

    class Chunk {
        +str id
        +str section
        +str kind
        +list~int~ paragraph_indices
        +str text
        +int claim_num
        +str translation
        +dict~int, str~ paragraph_translations
    }

    class ClaimPlan {
        +int claim_num
        +bool is_independent
        +tuple~int~ depends_on
        +str category
        +str required_preamble
    }

    class Prompt {
        +str name
        +str system
        +str user
        +build_messages(korean_text, context, lookahead) list~dict~
    }

    PatentTranslator --> ClientConfig
    PatentTranslator --> LLMClient
    PatentTranslator --> TranslationState
    TranslationState "1" o-- "*" ParagraphRecord
    TranslationState "1" o-- "*" Chunk
    TranslationState "1" o-- "*" ClaimPlan
    Chunk ..> ParagraphRecord : paragraph_indices
    ClaimPlan ..> Chunk : planned from claims
    LLMClient --> ClientConfig
```

## Claim Translation Detail

```mermaid
flowchart TD
    ClaimChunks[chunks_claims] --> Plans[build_claim_plans]
    Plans --> ParseDeps[parse_dependencies]
    Plans --> InferCategory[infer_category]
    Plans --> ResolvePreamble[resolve_plan_preamble]

    ResolvePreamble --> HasEquations{standalone equations?}

    HasEquations -->|no| WholeClaimPrompt[build_claim_messages]
    WholeClaimPrompt --> LLMWhole[LLM complete]
    LLMWhole --> ValidateWhole[validate_claim_preamble]
    ValidateWhole --> RepairWhole[repair_claim_preamble or repair prompt]
    RepairWhole --> ChunkTranslation[chunk.translation]

    HasEquations -->|yes| LayoutPrompt[build_claim_layout_messages]
    LayoutPrompt --> LLMLayout[LLM complete]
    LLMLayout --> SegmentMap[paragraph_translations]
    SegmentMap --> ValidateFirst[validate first segment preamble]
    ValidateFirst --> RepairFirst[repair first segment preamble]
    RepairFirst --> LayoutChunk[chunk.paragraph_translations]

    ChunkTranslation --> Write[write]
    LayoutChunk --> Write

    Write --> EquationBranch{paragraph_translations?}
    EquationBranch -->|yes| InPlace[_apply_claim_paragraph_translations]
    EquationBranch -->|no| MarkerSplit[_apply_claim_with_equations marker split]

    InPlace --> PreserveEquations[preserve original equation paragraphs]
    MarkerSplit --> PreserveEquations
```

## Module Responsibilities

```mermaid
classDiagram
    class main_py {
        +parse_args()
        +resolve_output()
        +main()
    }

    class batch_py {
        +_translate_file()
        +_scan_dirs()
        +_resolve_files()
        +_output_path_for()
        +main()
    }

    class translator_py {
        +PatentTranslator
    }

    class client_py {
        +ClientConfig
        +LLMClient
    }

    class graph_py {
        +build_graph()
    }

    class nodes {
        +load()
        +classify()
        +apply_static()
        +chunk_body()
        +translate_body()
        +chunk_abstract()
        +translate_abstract()
        +chunk_claims()
        +translate_claims()
        +make_decide()
        +make_revise()
        +write()
    }

    class docx_utils_py {
        +extract_all_text()
        +has_math()
        +has_drawing()
        +replace_text()
        +insert_para_after()
        +postprocess()
    }

    class claim_planning_py {
        +build_claim_plans()
        +parse_dependencies()
        +infer_category()
        +resolve_plan_preamble()
        +validate_claim_preamble()
        +repair_claim_preamble()
    }

    class prompts_py {
        +build_body_messages()
        +build_abstract_messages()
        +build_claim_messages()
        +build_claim_layout_messages()
        +build_preamble_repair_messages()
        +build_decision_messages()
        +build_revision_messages()
    }

    main_py --> translator_py
    batch_py --> translator_py
    translator_py --> graph_py
    translator_py --> client_py
    graph_py --> nodes
    nodes --> docx_utils_py
    nodes --> prompts_py
    nodes --> claim_planning_py
```

## Data Flow Summary

```mermaid
sequenceDiagram
    participant CLI as main.py / batch.py
    participant PT as PatentTranslator
    participant G as LangGraph
    participant N as Agent Nodes
    participant C as LLMClient
    participant D as DOCX

    CLI->>PT: translate_document(input, output)
    PT->>G: invoke(initial TranslationState)
    G->>N: load
    N->>D: read source DOCX
    G->>N: classify and static transforms
    G->>N: chunk body/abstract/claims
    N->>C: translate chunks with prompts
    C-->>N: JSON/text translations
    N->>N: update chunks, glossary, claim plans
    G->>N: optional review/revision
    N->>C: review or repair prompts
    C-->>N: revised text
    G->>N: write
    N->>D: replace translated text while preserving images/equations
    D-->>CLI: output DOCX
```
