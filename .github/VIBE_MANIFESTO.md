# **The Engineering Manager's Constitution for AI-Driven Development**

### **1. Philosophy: System Over Vibe**

AI is a high-performance engine; this Manifesto is the chassis. Speed without governance leads to "Software Mud" and security vulnerabilities. We prioritize systemic integrity and safety over rapid generation. We build secure assets, not just features.

### **2. Architectural Commandments**

### **Layer Integrity (Separation of Concerns)**

You must respect the **Separation of Concerns**. Cross-layer pollution is a critical failure.

- **API/Gateway Layer:** Only handles requests, validation, and responses. **NO** business logic, **NO** SQL/DB queries.
- **Business/Service Layer:** Contains all domain logic and rules. It orchestrates the flow.
- **Model/Data Layer:** The only place where raw data access (SQL, ORM, API calls) is allowed.

### **Security Integrity (Shift Left)**

Security is not an afterthought. It is a core constraint.

- **Zero Secrets Policy:** Never hardcode API keys, passwords, or tokens. Use environment variables or secret managers.
- **Input Sanitization:** Trust no input. All data from the API/Gateway layer must be validated and sanitized before reaching the Business or Model layers.
- **Least Privilege:** Logic must only have access to the data and resources strictly necessary for its function.
- **Secure Defaults:** Prefer secure configurations (e.g., HTTPS, encrypted cookies, hashed passwords) by default.

### **3. The VAP Protocol (Atomic Pipeline)**

You MUST execute every task following this sequence. No stage may be skipped.

### **PHASE 1: BLUEPRINT (Context & Security Mapping)**

- **Action:** Scan VAP_INDEX.json and identify the target layer.
- **Security Check:** Identify if this feature handles sensitive data (PII, Credentials, Financials).
- **Constraint:** State: *"This logic belongs in the [Layer]. Security risks identified: [List]."*

### **PHASE 2: DRAFTING (Engineered Code)**

- **Action:** Implement the feature using modular patterns.
- **Constraint:** Hardcoding a secret or skipping input validation is a **Protocol Violation**.

### **PHASE 3: AUDIT (The 0% Duplication & Security Boundary Rule)**

- **Action:** Cross-reference code for boundary violations and security flaws.
- **Requirement:** Check for SQL injection risks, XSS possibilities, and hardcoded secrets. If a risk is found, you MUST REFACTOR.

### **PHASE 4: PURGE (Systemic Cleaning)**

- **Action:** Sanitize the project. Remove dead code, debug logs, and update VAP_INDEX.json.

### **4. VAP Status Report (Mandatory Output)**

- [ ]  **Blueprint:** Impact zone, Layer Integrity, and **Security Risks** verified.
- [ ]  **Deduplication:** 0% Redundancy policy applied.
- [ ]  **Security:** Secrets checked, inputs sanitized, and least privilege applied.
- [ ]  **Refactoring:** Existing code modified to maintain integrity.
- [ ]  **Purge:** Dead code, debug logs, and temporary comments removed.