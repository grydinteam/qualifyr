// Thin client for the FastAPI backend. Base URL comes from NEXT_PUBLIC_API_URL.

import { createClient } from "@/lib/supabase/client"
import { supabaseConfigured } from "@/lib/supabase/config"

export const API_URL = process.env.NEXT_PUBLIC_API_URL
  ?? (typeof window !== "undefined" && window.location.hostname !== "localhost" ? "/api" : "http://localhost:8000")

// The monetization choice made at sign-up is stashed here until there is an authenticated
// session to save it against (accounts that need e-mail confirmation have no token yet).
export const PENDING_MONETIZATION_KEY = "qualifyr.pending_monetization"
// The optional free-text answer from sign-up, stashed alongside the vote until there is a token.
export const PENDING_MONETIZATION_COMMENT_KEY = "qualifyr.pending_monetization_comment"

export type CompanyType = "BUYER" | "VENDOR" | "UNKNOWN"
export type SequenceStatus =
  | "not_queued" | "queued" | "email_1_sent" | "followup_1_sent" | "followup_2_sent"
  | "completed" | "replied" | "bounced" | "unsubscribed" | "suppressed"
export type Step = "email_1" | "followup_1" | "followup_2"

export interface Lead {
  lead_id: string
  campaign_id: string
  company_name: string
  domain: string | null
  website: string | null
  country: string | null
  city: string | null
  industry: string | null
  company_description: string | null
  company_type: CompanyType
  buyer_fit_reason: string
  total_score: number
  score_reason: string
  contact_name: string | null
  contact_role: string | null
  contact_email: string | null
  email_status: string
  phone: string | null
  linkedin_or_public_profile_url: string | null
  personalization_hook: string | null
  research_brief?: string | null
  intent_fit?: boolean | null
  intent_confidence?: number
  intent_reason?: string
  buying_signal: string | null
  pain_signal: string | null
  source: string
  source_url: string | null
  outreach_ready: boolean
  sequence_status: SequenceStatus
  priority: "high_priority" | "qualified" | "review" | "reject"
  technologies: string[]
  phone_type: string | null
  candidate_email: string | null
  news_mentions: { title: string; url: string; date: string; source: string }[]
  intent_signals: { kind: string; source: string; source_url: string | null; text: string; organization: string | null; date: string | null; deadline: string | null; matched_terms: string[]; extracted: Record<string, string> | null }[]
  review_verdict: string | null
  reply_label: string | null
  reply_excerpt: string | null
  referred_contact: { name: string | null; email: string; status: string } | null
  domain_age_years: number | null
  provenance: Record<string, string>
  email_1_sent_at: string | null
  followup_1_at: string | null
  followup_2_at: string | null
  next_contact_at: string | null
  reply_status: string | null
  events?: OutreachEvent[]
  drafts?: Draft[]
}

export interface Draft {
  lead_id: string
  step: Step
  subject: string
  body: string
  status: "pending" | "approved" | "rejected" | "sent"
  edited: number
  created_at: string
  approved_at: string | null
}

export interface OutreachEvent {
  event_id: number
  lead_id: string
  event_type: string
  step: string | null
  detail: string | null
  created_at: string
  company_name?: string
  contact_email?: string
}

export interface CampaignCreate {
  name: string
  offer: string
  countries: string[]
  provinces: string[]
  cities: string[]
  target_industries: string[]
  buyer_keywords: string[]
  osm_categories: string[]
  overture_categories: string[]
  min_score: number
  max_companies: number
}

export interface Campaign {
  campaign_id: string
  name: string
  offer: string
  file: string | null
  cities: string[]
  countries: string[]
  provinces?: string[]
  areas?: string[]
  relevance_keywords?: string[]
  discovery_sectors?: string[]
  min_score: number
  max_companies: number
  leads: number
  buyers: number
  qualified: number
  outreach_ready: number
  last_run: { run_id: string; status: string; started_at: string; finished_at: string | null; stats_json: string | null } | null
  live: Progress | null
}

export interface Progress {
  run_id: string | null
  stage: string
  done: number
  total: number
  message: string
  stats: Record<string, number> | null
}

export interface Stats {
  campaign_id: string
  leads: number
  by_type: Record<CompanyType, number>
  by_status: Record<SequenceStatus, number>
  by_priority: Record<string, number>
  score_bands: Record<string, number>
  qualified: number
  outreach_ready: number
  emails_sent: number
  replied: number
  bounced: number
  reviewed: number
  correct: number
  accuracy: number | null
  verdicts: Record<string, number>
  with_intent: number
}

export interface QueueItem { lead: Lead; step: Step; draft: Draft }
export interface Queue {
  items: QueueItem[]
  counts: Record<SequenceStatus, number>
  smtp_configured: boolean
  daily_limit: number
  sent_today: number
  mailboxes: MailboxState[]
}

export interface Suppression { value: string; kind: string; reason: string | null; created_at: string }

export interface MailboxState {
  address: string
  auth_mode: string
  enabled: boolean
  days_active: number | null
  cap: number
  sent_today: number
  bounced_today: number
  remaining: number
  paused_reason: string | null
}

export interface SendReport {
  sent: number
  skipped: number
  failed: number
  stopped_reason: string | null
  details: string[]
  mode: string
  sync: Record<string, number | string> | null
  mailboxes?: Record<string, MailboxState>
}

/** The current Supabase access token, or null when signed out.
 *
 * Read per request rather than captured once: the SDK rotates the token in the background,
 * and a stale copy would start 401ing an hour into a session. getSession() reads local
 * storage and refreshes only when needed, so this is not a network call in the common case.
 */
async function accessToken(): Promise<string | null> {
  if (typeof window === "undefined" || !supabaseConfigured) return null
  try {
    const { data } = await createClient().auth.getSession()
    return data.session?.access_token ?? null
  } catch {
    return null
  }
}

/** On a 401, sign out the stale local session and redirect to sign-in at most once per
 * page-load. Shared by request() and the raw-fetch downloadExport so both honour the same
 * one-shot guard; without it, a download hitting a persistent 401 (JWKS mismatch, clock skew)
 * redirects on every call and loops. */
async function handleUnauthorized(): Promise<void> {
  if (typeof window === "undefined") return
  // Sign out the stale local session so the middleware stops thinking we're
  // authenticated (which would bounce /sign-in back to /dashboard → loop).
  try { const sb = (await import("@/lib/supabase/client")).createClient(); await sb.auth.signOut() } catch { /* best effort */ }
  const key = "__qualifyr_401_redirect"
  if (!sessionStorage.getItem(key)) {
    sessionStorage.setItem(key, "1")
    window.location.assign(`/sign-in?next=${encodeURIComponent(window.location.pathname)}`)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await accessToken()
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  })
  if (!res.ok) {
    if (res.status === 401) await handleUnauthorized()
    let detail = res.statusText
    try { detail = (await res.json()).detail ?? detail } catch { /* not json */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail))
  }
  // 204 No Content (e.g. DELETE) has an empty body; res.json() would throw on it.
  if (res.status === 204 || res.headers.get("content-length") === "0") return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string; version: string; smtp_configured: boolean; require_approval: boolean; auth_mode: string; warmup: { enabled: boolean; start: number; step: number; max: number }; limits?: { max_campaigns: number; max_leads_per_campaign: number } }>("/health"),
  campaigns: () => request<Campaign[]>("/campaigns"),
  createCampaign: (body: CampaignCreate) =>
    request<{ campaign_id: string; name: string }>("/campaigns", { method: "POST", body: JSON.stringify(body) }),
  createCampaignNL: (text: string, opts?: { max_companies?: number; osm_categories?: string[]; search_queries?: string[] }) =>
    request<{ campaign_id: string; config: Record<string, unknown>; explanation: Record<string, unknown>; status: string }>("/campaigns/nl", { method: "POST", body: JSON.stringify({ text, ...opts }) }),
  deleteCampaign: (id: string) =>
    request<void>(`/campaigns/${id}`, { method: "DELETE" }),
  runCampaign: (id: string, max_companies?: number) =>
    request<Progress>(`/campaigns/${id}/run`, { method: "POST", body: JSON.stringify({ max_companies }) }),
  progress: (id: string) => request<Progress>(`/campaigns/${id}/progress`),
  stats: (id: string) => request<Stats>(`/campaigns/${id}/stats`),
  leads: (id: string, q: { min_score?: number; company_type?: string; outreach_ready?: boolean; search?: string; order?: "recent" | "score"; limit?: number; offset?: number } = {}) => {
    const p = new URLSearchParams()
    if (q.min_score !== undefined) p.set("min_score", String(q.min_score))
    if (q.company_type) p.set("company_type", q.company_type)
    if (q.outreach_ready !== undefined) p.set("outreach_ready", String(q.outreach_ready))
    if (q.search) p.set("q", q.search)
    if (q.order) p.set("order", q.order)
    if (q.limit !== undefined) p.set("limit", String(q.limit))
    if (q.offset !== undefined) p.set("offset", String(q.offset))
    return request<{ items: Lead[]; total: number }>(`/campaigns/${id}/leads?${p}`)
  },
  lead: (leadId: string) => request<Lead>(`/leads/${leadId}`),
  suppress: (leadId: string, reason?: string) =>
    request<{ ok: boolean }>(`/leads/${leadId}/suppress`, { method: "POST", body: JSON.stringify({ reason }) }),
  exportUrl: (id: string, min_score = 70, buyers_only = true) =>
    `${API_URL}/campaigns/${id}/export?min_score=${min_score}&buyers_only=${buyers_only}`,
  /** Download the CSV through an authenticated fetch. A plain <a href> navigation cannot send
   * the bearer token, so the API answered "missing bearer token"; this fetches with the token,
   * then saves the returned blob. Honours the same filters shown in the Leads table. */
  downloadExport: async (id: string, opts: { min_score?: number; company_type?: string } = {}) => {
    const token = await accessToken()
    const p = new URLSearchParams()
    if (opts.min_score !== undefined) p.set("min_score", String(opts.min_score))
    if (opts.company_type) p.set("company_type", opts.company_type)
    const res = await fetch(`${API_URL}/campaigns/${id}/export?${p}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      cache: "no-store",
    })
    if (!res.ok) {
      if (res.status === 401) await handleUnauthorized()
      let detail = res.statusText
      try { detail = (await res.json()).detail ?? detail } catch { /* not json */ }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail))
    }
    const blob = await res.blob()
    const cd = res.headers.get("content-disposition") ?? ""
    const match = /filename="?([^"]+)"?/.exec(cd)
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = match ? match[1] : `${id}.csv`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  },
  queue: (id: string) => request<Queue>(`/campaigns/${id}/outreach/queue`),
  draft: (leadId: string, step: Step) => request<Draft>(`/leads/${leadId}/drafts/${step}`),
  saveDraft: (leadId: string, step: Step, subject: string, body: string) =>
    request<Draft>(`/leads/${leadId}/drafts/${step}`, { method: "PUT", body: JSON.stringify({ subject, body }) }),
  approve: (leadId: string, step: Step) => request<Draft>(`/leads/${leadId}/drafts/${step}/approve`, { method: "POST" }),
  reject: (leadId: string, step: Step) => request<Draft>(`/leads/${leadId}/drafts/${step}/reject`, { method: "POST" }),
  resetDraft: (leadId: string, step: Step) => request<Draft>(`/leads/${leadId}/drafts/${step}/reset`, { method: "POST" }),
  send: (id: string, body: { limit?: number; dry_run?: boolean; ignore_window?: boolean }) =>
    request<SendReport>(`/campaigns/${id}/outreach/send`, { method: "POST", body: JSON.stringify(body) }),
  sync: (id: string) => request<SendReport["sync"] & { details: string[] }>(`/campaigns/${id}/outreach/sync`, { method: "POST" }),
  activity: (id: string) => request<OutreachEvent[]>(`/campaigns/${id}/outreach/activity`),
  suppressions: () => request<Suppression[]>("/suppressions"),
  addSuppression: (value: string, reason?: string) =>
    request<{ ok: boolean }>("/suppressions", { method: "POST", body: JSON.stringify({ value, reason }) }),
  removeSuppression: (value: string) => request<{ ok: boolean }>(`/suppressions/${encodeURIComponent(value)}`, { method: "DELETE" }),
  mailboxes: (campaignId?: string) => request<MailboxState[]>(`/mailboxes${campaignId ? `?campaign_id=${campaignId}` : ""}`),
  campaignYaml: (id: string) => request<{ campaign_id: string; file: string; yaml: string }>(`/campaigns/${id}/yaml`),
  validateCampaign: (yaml: string) =>
    request<{ ok: boolean; error?: string; campaign_id: string; name: string; sources: string[] }>("/campaigns/validate", { method: "POST", body: JSON.stringify({ yaml }) }),
  saveCampaignYaml: (id: string, yaml: string) =>
    request<{ ok: boolean; file: string | null; campaign_id?: string }>(`/campaigns/${id}/yaml`, { method: "PUT", body: JSON.stringify({ yaml }) }),
  // Settings: user mailboxes
  listUserMailboxes: () =>
    request<{ mailboxes: { address: string; smtp_host: string; smtp_port: number; sender_name: string | null; daily_limit: number | null; enabled: boolean; created_at: string }[]; encryption_available: boolean }>("/settings/mailboxes"),
  saveUserMailbox: (body: { address: string; password: string; smtp_host?: string; smtp_port?: number; sender_name?: string; daily_limit?: number }) =>
    request<{ ok: boolean; address: string }>("/settings/mailboxes", { method: "PUT", body: JSON.stringify(body) }),
  deleteUserMailbox: (address: string) =>
    request<{ ok: boolean }>(`/settings/mailboxes/${encodeURIComponent(address)}`, { method: "DELETE" }),
  toggleUserMailbox: (address: string) =>
    request<{ ok: boolean; enabled: boolean }>(`/settings/mailboxes/${encodeURIComponent(address)}/toggle`, { method: "POST" }),
  testUserMailbox: (body: { address: string; password: string; smtp_host?: string; smtp_port?: number }) =>
    request<{ ok: boolean; message: string }>("/settings/mailboxes/test", { method: "POST", body: JSON.stringify(body) }),

  sheetsStatus: () => request<{ configured: boolean; spreadsheet_id: string | null }>("/sheets/status"),
  exportSheets: (id: string) => request<{ rows: number; tab: string; url: string }>(`/campaigns/${id}/export/sheets`, { method: "POST" }),
  review: (leadId: string, verdict: string) =>
    request<{ ok: boolean; review_verdict: string | null }>(`/leads/${leadId}/review`, { method: "POST", body: JSON.stringify({ verdict }) }),
  referral: (leadId: string, accept: boolean) =>
    request<{ ok: boolean }>(`/leads/${leadId}/referral`, { method: "POST", body: JSON.stringify({ accept }) }),
  sequence: (id: string) => request<Lead[]>(`/campaigns/${id}/outreach/sequence`),

  // Settings: API keys
  listApiKeys: () => request<{ keys: { key_name: string; created_at: string }[]; encryption_available: boolean }>("/settings/api-keys"),
  saveApiKey: (name: string, value: string) =>
    request<{ ok: boolean; key_name: string }>(`/settings/api-keys/${name}`, { method: "PUT", body: JSON.stringify({ value }) }),
  deleteApiKey: (name: string) =>
    request<{ ok: boolean }>(`/settings/api-keys/${name}`, { method: "DELETE" }),
  testApiKey: (name: string) =>
    request<{ ok: boolean; message: string }>(`/settings/api-keys/${name}/test`, { method: "POST" }),

  // Settings: usage
  getUsage: () => request<{ usage: Record<string, { count: number; limit: number; default_limit: number; max_limit: number }> }>("/settings/usage"),
  updateUsageLimit: (resource: string, limit: number) =>
    request<{ ok: boolean; resource: string; limit: number }>(`/settings/usage/${resource}`, { method: "PUT", body: JSON.stringify({ limit }) }),

  // Settings: preferences
  myLimits: () => request<{ unlimited: boolean; max_campaigns: number | null; max_leads_per_campaign: number | null }>("/settings/limits"),
  getPreferences: () => request<{ preferences: Record<string, string> }>("/settings/preferences"),
  setPreference: (key: string, value: string) =>
    request<{ ok: boolean }>(`/settings/preferences/${key}`, { method: "PUT", body: JSON.stringify({ value }) }),
}

export const STEP_LABEL: Record<Step, string> = {
  email_1: "Email 1", followup_1: "Follow-up 1", followup_2: "Follow-up 2",
}
