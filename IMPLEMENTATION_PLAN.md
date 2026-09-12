# Personal Job Application Monitor — Implementation Plan

Prepared September 11, 2026. Audited September 12, 2026. Status: the core local workflow and
phases 2–6 are implemented and covered by automated checks. Final release validation still
requires the user's real Gmail OAuth/mailbox pilot and browser-site capture checks.

Implementation notes from the audit:

- Review supports evidence-backed accept, explicit link, create, ignore, and compatible bulk
  actions. Splitting one source email into several unrelated events remains a documented model
  limitation; application merge is an explicit dry-run command because company/title-only merges
  can destroy legitimate repeat applications.
- Settings supports previewed historical rescans and retrying stored failures. Importing one
  arbitrary Gmail message ID is not implemented; add it only if the real-mail pilot finds messages
  that the deliberately broad Gmail query cannot recover.
- Interview preparation uses the existing task and notes records instead of a second checklist
  model. Full-profile deletion remains an offline administrative operation rather than a one-click
  destructive Settings action.
- The supplied user-level timers provide five-minute Gmail sync and daily backups. They must be
  installed on the target Linux account, and secrets/OAuth authorization require user action.

## 1. Agreed scope

Build a single-user application that runs on your computer and opens in your browser. Detect applications and updates through Gmail. Include follow-up reminders, interview scheduling, notes and contacts, resume and cover-letter versions, progress reports, and permanent local copies of job postings and requirements. Cloud AI may process relevant content.

Defaults to validate during setup: Linux, matching the current workspace; Chromium-compatible browser for the capture extension; English interface; Africa/Cairo display timezone. These choices do not change the core data model. Multiple Gmail accounts can each have their own connection and sync state.

Success means you can open the app and immediately see what you applied to, what changed, what needs action, and exactly what each saved job required.

## 2. Research and recommended approach

Use a searchable application table alongside a stage board and a Today screen. Teal demonstrates application tracking around stages, follow-ups, and application details; Huntr provides a Kanban-style workflow. These support the proposed interaction patterns, rather than proving one tool is universally best. [Teal](https://help.tealhq.com/en/articles/14435727-how-to-track-your-job-applications), [Huntr](https://help.huntr.co/en/articles/10477521-what-is-huntr).

Combine automatic Gmail detection with a user-triggered browser capture. Email provides evidence of applications and progress; capturing the open posting preserves information that confirmation messages may omit. Email alone cannot guarantee coverage of applications that generate no message, and deleted postings cannot always be recovered. Manual entry, pasted descriptions, and imported files remain available.

## 3. Screens and complete feature scope

| Screen | Planned behavior |
| --- | --- |
| Today | Due and overdue follow-ups, upcoming interviews, new updates, uncertain imports, missing job descriptions, Gmail sync health. |
| Applications | Searchable, sortable table plus stage board; filters for company, role, stage, source, date, location, work arrangement, priority, and tags. |
| Application detail | Overview, saved posting, requirements, chronological activity, linked emails, contacts, interviews, tasks, and exact document versions used. |
| Review inbox | Confirm, correct, ignore, merge, or split uncertain AI results and suggested application matches; show source evidence beside suggestions. |
| Calendar and tasks | Interview rounds, deadlines, preparation tasks, reminders, rescheduling, completion, and calendar-file export. |
| Documents and contacts | Upload/version resumes and cover letters; record recruiter details, contact history, and notes. |
| Reports | Applications over time, current stage distribution, interview/offer rates, response times, and comparisons by source and resume version. |
| Settings | Gmail connections, import date range, timezone, reminder defaults, AI configuration and usage, capture-extension pairing, backups, export, and deletion. |

Default stages: Saved → Preparing → Applied → Screening → Interviewing → Offer → Accepted, with Rejected and Withdrawn available. Record interview rounds separately. Allow manual correction and reopening; never infer rejection from silence or treat stages as an irreversible ladder.

Application fields include company, title, job URL, requisition ID, source, location, work arrangement, employment type, salary range/currency/period, application date, closing date, priority, tags, notes, and current stage. Unknown values remain unknown. Saving a vacancy does not count as submitting an application.

## 4. Gmail capture and trustworthy automation

1. Connect through Google's official OAuth flow with read-only Gmail access. Store credentials in the operating system credential store; never request the Gmail password.
2. Offer a configurable initial history window, defaulting to 90 days, with a preview of import scope and progress. Include archived mail and sent messages within the chosen scope; exclude spam/trash by default and make that limitation visible.
3. Identify candidate messages using sender, subject, headers, and inexpensive text rules. Use cloud AI to distinguish confirmations, recruiter contact, assessments, interviews, offers, rejections, withdrawals, and unrelated messages. Provide a broader rescan and manual message import for missed candidates.
4. Extract structured fields with supporting excerpts: company, role, requisition ID, event type, relevant dates, links, and contacts. Reject invalid structured responses; missing dates and entities are not invented.
5. Match using requisition ID and known job links first, then thread context and company/title evidence. Company name alone is insufficient. Several applications to the same company must remain separate.
6. Automatically create/update records only when evidence and matching rules are unambiguous. Ambiguous matches, conflicts, and interview times needing clarification enter the review inbox. AI confidence alone is insufficient authorization for an update.
7. Keep an event history with message provenance. Protect user corrections from later imports. Older messages must not regress the current stage; deleting an email must not delete the application.
8. Sync approximately every five minutes while running, plus on launch and on demand. Use Gmail history-based incremental sync, pagination, retry/backoff, and a single active sync per account. Store changes and checkpoints safely; repeated messages must not produce duplicate applications or reminders.
9. If a history cursor expires, recover with a scoped rescan and deduplication. Show the last successful sync, errors, pending work, and a reconnect action.

Gmail documents incremental history sync and the need to resynchronize after an expired history ID. [Gmail synchronization](https://developers.google.com/workspace/gmail/api/guides/sync).

OAuth setup is the first technical feasibility gate. Gmail read access is restricted; personal-use exceptions and account policies must be checked for the actual setup. External OAuth apps left in Testing can receive refresh tokens that expire after seven days, so the plan must not assume unattended access will last indefinitely. [Scope requirements](https://developers.google.com/workspace/gmail/api/auth/scopes), [Verification exceptions](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification), [Token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

## 5. Saving postings and requirements

Provide a small browser extension with a Save job button. When clicked, capture the visible job content and available structured job data, then present an editable preview before storing it locally. Use temporary active-tab access rather than continuous access to every browsing session. [Chrome activeTab](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab).

Also attempt retrieval of recognized public job links found in relevant emails. Extract structured JobPosting data when present, then readable page text. Sites can omit structured fields, so this is a fallback chain rather than a guarantee. [JobPosting fields](https://schema.org/JobPosting).

For each snapshot, save:

- Original and resolved URL, source, capture time, title, company, and available job identifier.
- Full extracted job description and requirements text, preserved independently from AI summaries.
- Responsibilities, required versus preferred skills, qualifications, experience, location/work eligibility, benefits, and compensation when explicitly stated.
- Source excerpts for extracted requirements and an explicit unknown state for missing information.
- A local printable snapshot; allow an original PDF or screenshot to be attached when visual fidelity matters.

Keep snapshots immutable and dated. Recapture creates a new version; the version associated with an application remains available. Search the stored descriptions and requirements offline. Clearly label partial captures and failures. For login-protected or blocked pages, offer the browser button, paste, or file upload; do not silently store a URL as though the description were archived.

## 6. Follow-ups, interviews, documents, and reporting

Follow-ups: editable suggested dates, snooze/complete actions, overdue indicators, and desktop notifications. Reminder defaults are user settings, not claims that every employer should be contacted on the same schedule. Provide editable follow-up drafts to copy; the tracker does not automatically send email.

Interviews: separate rounds with participants, format, meeting/location link, original timezone, start/end times, prep checklist, notes, and outcome. Detect invitations and propose events; flag ambiguous dates and handle cancellations/reschedules. Export .ics files for the user's calendar; two-way calendar synchronization is outside this version.

Documents: copy uploaded files into managed local storage instead of relying on fragile original paths. Version resumes and cover letters; link the exact version to each application. Permit manual identification when Gmail cannot prove which file was submitted. Store attachment hashes to avoid duplicate file storage.

Reports: distinguish current stage counts from historical milestones. Interview rate means submitted applications that ever reached interview divided by submitted applications in the selected application-date cohort; offer rate uses the same denominator. Exclude acknowledgments from substantive response times. Show sample sizes and pending outcomes, and avoid implying source/resume comparisons prove causation.

## 7. Architecture and data model

Recommended stack: Python with Django, server-rendered HTML/CSS and small amounts of JavaScript, SQLite, local file storage, a Chromium extension, and the selected cloud AI provider's official client. Django supplies forms, database migrations, and session/security foundations; SQLite suits a personal local application. [Django overview](https://www.djangoproject.com/start/overview/), [SQLite use cases](https://www.sqlite.org/whentouse.html).

Run a local web process and one background worker, started by a user-level operating system service. Persist work and schedules in SQLite. No separate hosted backend, Redis, container platform, or microservices are needed for this scope. Keep the browser interface usable while the worker syncs. Use short database transactions and SQLite's backup facilities.

| Record | Purpose |
| --- | --- |
| Gmail account | Account identity, credential reference, sync cursor, connection state. |
| Application | Opportunity identity, current stage, dates, searchable fields, user corrections. |
| Email | Unique account/message identity, thread, relevant text, source metadata, processing state. |
| Application event | Source-linked historical change, effective time, import time, correction record. |
| Posting snapshot | Immutable source content, structured requirements, capture state and version. |
| Task | Due time, application link, status, notification delivery state. |
| Interview | Round, schedule, timezone, participants, location, outcome and notes. |
| Contact | Recruiter/interviewer information and application associations. |
| Document | Local file reference, hash, version label and application associations. |
| Work item | Retryable sync/extraction work, attempts, completion/error state. |

Use database uniqueness constraints for imported messages and source events. Apply changes transactionally. Make merge/split operations reviewable and preserve provenance. Begin with ordinary indexed search; add SQLite full-text search only if description search performance requires it.

## 8. Privacy, reliability, and operation

- Bind the service only to loopback. Protect local access with a session, strict host/origin validation and CSRF protection. Pair the extension using a revocable credential; arbitrary websites must not be able to write into the tracker.
- Render email and page content as inert text or sanitized content. Do not load remote email images. Treat source text as data, including instructions embedded in emails or postings.
- Restrict automatic retrieval to recognized job-posting URLs; validate redirects and destinations, block private/local addresses, cap response size/time, and avoid fetching unsubscribe, tracking, or action links.
- Keep Gmail tokens and AI keys outside the database and exports. Restrict local file permissions. Local storage is not automatically encrypted; document use of the operating system's disk encryption and encrypted backup options.
- Send only relevant message/posting content to AI, removing unrelated quoted history and signatures where practical. Do not send resume files unless a specific extraction action needs them. Show provider/model configuration and disclose its applicable retention policy before enabling processing.
- Select the model after a small extraction benchmark. Cache results by content hash and extraction version; track usage and stop new requests once recorded spend reaches a configurable monthly ceiling. At the limit or during an outage, queue extraction and keep manual tracking available.
- Back up the database and referenced files consistently, daily when running and before migrations. Keep a bounded rotation; provide a full export and a tested restore flow. CSV is a summary export, not a complete backup. Exclude credentials and require reconnecting after restoration on another machine.
- Run in the background after the browser closes. While the computer is off/asleep, Gmail sync and local notifications pause; on resume, catch up and show a consolidated overdue summary without duplicate alerts.

## 9. Implementation sequence and completion gates

Effort estimates are planning ranges for focused development, not delivery commitments; actual OAuth setup and supported job sites can change them.

| Phase | Work | Completion gate | Estimate |
| --- | --- | --- | --- |
| 1. Feasibility | Gmail OAuth, credential storage, read a small chosen sample, test one AI model and representative posting capture. | Confirm access, extraction quality, privacy settings, and realistic capture limitations. | 1–2 days |
| 2. Local foundation | Database, application CRUD, table/board/detail, activity history, document storage, launcher. | A manually entered application survives restart with its documents and history. | 2–3 days |
| 3. Gmail automation | Backfill, incremental worker, matching, extraction, review inbox, corrections, retries. | Replaying messages creates no duplicates; ambiguous messages remain reviewable. | 3–5 days |
| 4. Posting archive | Browser capture, public-link extraction, immutable snapshots, requirements extraction/search. | A saved posting remains readable offline after its source disappears. | 2–4 days |
| 5. Daily workflow | Tasks, reminders, interviews, calendar export, contacts, document versions, reports. | Complete an application-to-interview-to-outcome scenario and verify report totals. | 2–4 days |
| 6. Finish and verify | Background startup, security checks, backup/restore, accessibility, setup guide, real-mail pilot. | Restore into a clean local setup and complete a monitored pilot without data loss. | 2–3 days |

Estimated total: 12–21 focused development days. All requested features remain in scope; phases are delivery order, not a reduction to a manual-only tracker. No hosting bill is required by this design. AI usage, optional backup storage, and any provider-specific costs depend on configuration; measure them during phase 1.

## 10. Validation and release criteria

Use a small anonymized or synthetic fixture set covering confirmations, newsletters, rejections, interview invitations, multiple roles at one company, forwarded messages, and missing details. Supplement with user-reviewed real messages during the pilot; do not claim a detection accuracy percentage before measuring it.

Automated checks must cover duplicate replay, expired sync cursor recovery, interrupted imports, out-of-order emails, protected manual corrections, uncertain matches, AI failures, unsafe links/content, notification deduplication, timezone/rescheduling, snapshot preservation, report denominators, and full backup restoration. Use the framework's built-in testing tools and a small browser smoke test; keep tests focused on behavior and data-loss risks.

Release when the user can connect Gmail, review a historical import, receive new updates, recover from disconnection, save and reopen a posting offline, correct an extraction, manage interviews/follow-ups, identify the resume submitted, export reports, and restore the complete archive. Keyboard operation and readable error/empty/loading states are required.

## 11. Explicit boundaries and setup decisions

This is an application monitor with the complete requested tracking workflow. Automated job submission, outbound email sending, resume generation, social-network scraping, mobile synchronization, and multi-user hosting are separate features. They are not required to deliver the agreed scope.

Before implementation begins, confirm the actual browser/OS and choose the cloud AI provider, model, and spending limit. Gmail OAuth consent and any account-policy approvals require the user's interaction. These are setup inputs; the architecture and implementation sequence above are ready to use.
