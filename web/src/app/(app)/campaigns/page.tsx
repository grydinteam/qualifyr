"use client"

import * as React from "react"
import { useRouter } from "next/navigation"
import { Play, Download, Plus, Trash2, Pencil, Maximize2, Minimize2 } from "lucide-react"
import { cn } from "@/lib/utils"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Progress } from "@/components/ui/progress"
import { Badge } from "@/components/ui/badge"
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { api, type Campaign, type Progress as RunProgress } from "@/lib/api"
import { useCampaign } from "@/components/campaign-context"

function ExpandButton({ expanded, onToggle }: { expanded: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className="absolute top-3 right-11 inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted"
      title={expanded ? "Collapse" : "Expand"}
    >
      {expanded ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
      <span className="sr-only">{expanded ? "Collapse" : "Expand"} panel</span>
    </button>
  )
}

function NewCampaign({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (campaignId: string) => void }) {
  // NL mode state
  const [text, setText] = React.useState("")
  // Manual mode state
  const [name, setName] = React.useState("")
  const [offer, setOffer] = React.useState("")
  const [industries, setIndustries] = React.useState("")
  const [cities, setCities] = React.useState("")
  const [keywords, setKeywords] = React.useState("")
  const [categories, setCategories] = React.useState("")
  const [searchQueries, setSearchQueries] = React.useState("")
  // Shared state
  const [maxCo, setMaxCo] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [result, setResult] = React.useState<{ campaignId: string; config: Record<string, unknown>; explanation: Record<string, unknown> | string } | null>(null)
  // Key availability
  const [hasGroq, setHasGroq] = React.useState<boolean | null>(null)
  const [hasBrave, setHasBrave] = React.useState<boolean | null>(null)
  // Free-tier per-campaign lead cap, read from the backend (not hardcoded).
  const [maxLeads, setMaxLeads] = React.useState<number | null>(null)
  const [expanded, setExpanded] = React.useState(false)

  React.useEffect(() => {
    if (!open) return
    api.listApiKeys().then((r) => {
      const names = new Set(r.keys.map((k) => k.key_name))
      setHasGroq(names.has("groq"))
      setHasBrave(names.has("brave"))
    }).catch(() => { setHasGroq(false); setHasBrave(false) })
    api.myLimits().then((l) => setMaxLeads(l.max_leads_per_campaign)).catch(() => {})
  }, [open])

  const nlMode = hasGroq === true

  const submitNL = async () => {
    setError(null)
    if (!text.trim()) { setError("Describe what you're looking for."); return }
    setBusy(true)
    try {
      const mc = maxCo.trim() ? parseInt(maxCo, 10) : undefined
      const split = (s: string) => s.split(",").map((t) => t.trim()).filter(Boolean)
      const res = await api.createCampaignNL(text.trim(), {
        ...(mc ? { max_companies: mc } : {}),
        ...(categories.trim() ? { osm_categories: split(categories) } : {}),
        ...(searchQueries.trim() ? { search_queries: split(searchQueries) } : {}),
      })
      setResult({ campaignId: res.campaign_id, config: res.config, explanation: res.explanation })
      const returned = typeof res.explanation === "object" && res.explanation?.max_companies
      if (returned) setMaxCo(String(returned))
      onCreated(res.campaign_id)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  const submitManual = async () => {
    setError(null)
    if (!name.trim() || !offer.trim()) { setError("Name and offer are required."); return }
    setBusy(true)
    try {
      const body = {
        name: name.trim(),
        offer: offer.trim(),
        countries: ["Pakistan"],
        provinces: [] as string[],
        cities: cities.trim() ? cities.split(",").map((s) => s.trim()).filter(Boolean) : [],
        target_industries: industries.trim() ? industries.split(",").map((s) => s.trim()).filter(Boolean) : [],
        buyer_keywords: keywords.trim() ? keywords.split(",").map((s) => s.trim()).filter(Boolean) : [],
        osm_categories: categories.trim() ? categories.split(",").map((s) => s.trim()).filter(Boolean) : [],
        overture_categories: [] as string[],
        min_score: 70,
        max_companies: maxCo.trim() ? parseInt(maxCo, 10) : 60,
      }
      const res = await api.createCampaign(body)
      setResult({ campaignId: res.campaign_id, config: body as unknown as Record<string, unknown>, explanation: `Campaign "${res.name}" created` })
      onCreated(res.campaign_id)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  const runNow = async () => {
    if (!result) return
    try {
      await api.runCampaign(result.campaignId, Number(maxCo) || undefined)
    } catch { /* run will show in the campaign card */ }
    close()
  }

  const close = () => {
    setText(""); setName(""); setOffer(""); setIndustries(""); setCities("")
    setKeywords(""); setCategories(""); setSearchQueries(""); setMaxCo("")
    setError(null); setResult(null); onClose()
  }

  const exp = result?.explanation
  const rows: { label: string; value: string }[] = []
  if (exp && typeof exp === "object") {
    const e = exp as Record<string, unknown>
    if (e.name) rows.push({ label: "Name", value: String(e.name) })
    if (e.offer_detected || e.offer) rows.push({ label: "Offer", value: String(e.offer_detected ?? e.offer) })
    if (Array.isArray(e.cities) && e.cities.length) rows.push({ label: "Cities", value: e.cities.join(", ") })
    if (Array.isArray(e.areas) && e.areas.length) rows.push({ label: "Areas", value: e.areas.join(", ") })
    if (Array.isArray(e.provinces) && e.provinces.length) rows.push({ label: "Provinces", value: e.provinces.join(", ") })
    if (Array.isArray(e.target_industries) && e.target_industries.length) rows.push({ label: "Industries", value: e.target_industries.join(", ") })
    if (Array.isArray(e.buyer_keywords) && e.buyer_keywords.length) rows.push({ label: "Keywords", value: e.buyer_keywords.join(", ") })
    if (Array.isArray(e.sectors_matched) && e.sectors_matched.length) rows.push({ label: "Sectors", value: e.sectors_matched.join(", ") })
    if (Array.isArray(e.search_queries) && e.search_queries.length) rows.push({ label: "Queries", value: e.search_queries.join("; ") })
  }

  const loading = hasGroq === null

  return (
    <Sheet open={open} onOpenChange={(o) => !o && close()}>
      <SheetContent side="right" className={cn("w-full overflow-y-auto transition-[max-width] duration-200", expanded ? "sm:max-w-3xl" : "sm:max-w-lg")}>
        <ExpandButton expanded={expanded} onToggle={() => setExpanded((v) => !v)} />
        <SheetHeader><SheetTitle>New campaign</SheetTitle></SheetHeader>
        <div className="flex flex-col gap-4 p-4 pt-0">
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : nlMode ? (
            <>
              <p className="text-sm text-muted-foreground">Describe what you&apos;re looking for in plain English. The engine figures out the cities, industries, and search categories automatically.</p>
              <Textarea
                rows={4}
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="Find grocery stores in Islamabad that need inventory management software"
                autoFocus
              />
              {!hasBrave && !result && (
                <div className="rounded-lg border border-dashed p-3 grid gap-2">
                  <p className="text-xs font-medium text-muted-foreground">No Brave API key – provide search hints to improve discovery:</p>
                  <Input
                    value={categories}
                    onChange={(e) => setCategories(e.target.value)}
                    placeholder="OSM categories: shop=supermarket, shop=convenience"
                    className="text-xs"
                  />
                  <Input
                    value={searchQueries}
                    onChange={(e) => setSearchQueries(e.target.value)}
                    placeholder="Search queries: grocery stores Islamabad, marts near F-11"
                    className="text-xs"
                  />
                </div>
              )}
              {!result && (
                <div className="flex flex-wrap gap-1.5">
                  {["find bakeries in Lahore", "grocery stores in Islamabad needing POS systems", "clothing retailers in Karachi without an online store"].map((ex) => (
                    <button key={ex} type="button" onClick={() => setText(ex)} className="rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground hover:bg-muted transition-colors">
                      {ex}
                    </button>
                  ))}
                </div>
              )}
            </>
          ) : (
            <>
              <div className="rounded-lg border border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-950/30 p-3">
                <p className="text-xs text-blue-700 dark:text-blue-300">Add a <strong>Groq API key</strong> in Settings → API Keys to unlock automatic mode – describe what you want in plain English and the engine handles the rest.</p>
              </div>
              <div className="grid gap-3">
                <div className="grid gap-1.5">
                  <label className="text-xs font-medium">Campaign name *</label>
                  <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Grocery stores Islamabad" autoFocus />
                </div>
                <div className="grid gap-1.5">
                  <label className="text-xs font-medium">What you sell / offer *</label>
                  <Textarea rows={2} value={offer} onChange={(e) => setOffer(e.target.value)} placeholder="POS and inventory management software for retail stores" />
                </div>
                <div className="grid gap-1.5">
                  <label className="text-xs font-medium">Target industries <span className="text-muted-foreground font-normal">(comma-separated)</span></label>
                  <Input value={industries} onChange={(e) => setIndustries(e.target.value)} placeholder="retail, grocery, supermarket" />
                </div>
                <div className="grid gap-1.5">
                  <label className="text-xs font-medium">Cities <span className="text-muted-foreground font-normal">(comma-separated)</span></label>
                  <Input value={cities} onChange={(e) => setCities(e.target.value)} placeholder="Islamabad, Rawalpindi" />
                </div>
                <div className="grid gap-1.5">
                  <label className="text-xs font-medium">Buyer keywords <span className="text-muted-foreground font-normal">(comma-separated)</span></label>
                  <Input value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder="store, mart, shop, retailer" />
                </div>
                {!hasBrave && (
                  <div className="grid gap-1.5">
                    <label className="text-xs font-medium">OSM categories <span className="text-muted-foreground font-normal">(comma-separated, e.g. shop=supermarket)</span></label>
                    <Input value={categories} onChange={(e) => setCategories(e.target.value)} placeholder="shop=supermarket, shop=convenience" />
                  </div>
                )}
              </div>
            </>
          )}
          <div className="flex items-center gap-2">
            <label htmlFor="max-co" className="text-sm text-muted-foreground whitespace-nowrap">Max companies</label>
            <input
              id="max-co"
              type="number"
              min={1}
              max={maxLeads ?? 500}
              value={maxCo}
              onChange={(e) => setMaxCo(e.target.value)}
              placeholder={String(maxLeads ?? 30)}
              className="w-20 rounded-md border bg-transparent px-2 py-1 text-sm"
            />
            {maxLeads != null && (
              <span className="text-xs text-muted-foreground">Free tier: up to {maxLeads} per campaign</span>
            )}
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
          {result && (
            <div className="rounded-lg border bg-muted/30 p-3">
              <p className="text-sm font-medium mb-3">Campaign created</p>
              {typeof exp === "string" ? (
                <p className="text-sm text-muted-foreground">{exp}</p>
              ) : (
                <div className="grid gap-2">
                  {rows.map((r) => (
                    <div key={r.label} className="flex gap-2 text-sm">
                      <span className="shrink-0 font-medium text-muted-foreground w-28">{r.label}</span>
                      <span className="break-words min-w-0">{r.value}</span>
                    </div>
                  ))}
                </div>
              )}
              <div className="mt-3 flex gap-2">
                <Button size="sm" onClick={runNow}><Play data-icon="inline-start" /> Run now</Button>
                <p className="text-xs text-muted-foreground self-center">Starts discovery immediately. Progress shows on the campaign card.</p>
              </div>
            </div>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={close} disabled={busy}>Cancel</Button>
            <Button onClick={nlMode ? submitNL : submitManual} disabled={busy || loading}>
              {busy ? "Creating…" : result ? "Recreate campaign" : "Create campaign"}
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  )
}

function EditCampaign({ campaign, open, onClose, onSaved }: { campaign: Campaign; open: boolean; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = React.useState("")
  const [offer, setOffer] = React.useState("")
  const [industries, setIndustries] = React.useState("")
  const [cities, setCities] = React.useState("")
  const [areas, setAreas] = React.useState("")
  const [keywords, setKeywords] = React.useState("")
  const [categories, setCategories] = React.useState("")
  const [queries, setQueries] = React.useState("")
  const [maxCo, setMaxCo] = React.useState("")
  const [minScore, setMinScore] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [loading, setLoading] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [raw, setRaw] = React.useState<Record<string, unknown>>({})
  const [expanded, setExpanded] = React.useState(false)

  React.useEffect(() => {
    if (!open) return
    setLoading(true)
    setError(null)
    api.campaignYaml(campaign.campaign_id).then((r) => {
      const parsed = parseYaml(r.yaml)
      const geo = parsed.geography && typeof parsed.geography === "object" ? parsed.geography as Record<string, unknown> : null
      setRaw(parsed)
      setName(String(parsed.name ?? ""))
      setOffer(String(parsed.offer ?? ""))
      setIndustries(arr(parsed.target_industries).join(", "))
      setCities(arr(geo ? geo.cities : parsed.cities).join(", "))
      setAreas(arr(geo ? geo.areas : undefined).join(", "))
      setKeywords(arr(parsed.buyer_keywords).join(", "))
      setCategories(arr(parsed.osm_categories).join(", "))
      setQueries(arr(parsed.search_queries).join(", "))
      setMaxCo(String(parsed.max_companies ?? ""))
      setMinScore(String(parsed.min_score ?? ""))
    }).catch((e) => setError((e as Error).message)).finally(() => setLoading(false))
  }, [open, campaign.campaign_id])

  const save = async () => {
    setError(null)
    if (!name.trim() || !offer.trim()) { setError("Name and offer are required."); return }
    setBusy(true)
    try {
      const updated: Record<string, unknown> = {
        ...raw,
        name: name.trim(),
        offer: offer.trim(),
        target_industries: split(industries),
        buyer_keywords: split(keywords),
        osm_categories: split(categories),
        search_queries: split(queries),
        max_companies: maxCo.trim() ? parseInt(maxCo, 10) : raw.max_companies,
        min_score: minScore.trim() ? parseInt(minScore, 10) : raw.min_score,
      }
      const geo: Record<string, unknown> = typeof raw.geography === "object" && raw.geography ? { ...raw.geography as Record<string, unknown> } : { countries: ["Pakistan"] }
      geo.cities = split(cities)
      geo.areas = split(areas)
      updated.geography = geo
      const yaml = toYaml(updated)
      const res = await api.saveCampaignYaml(campaign.campaign_id, yaml)
      if (!res.ok) { setError("Save failed"); return }
      onSaved()
      onClose()
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className={cn("w-full overflow-y-auto transition-[max-width] duration-200", expanded ? "sm:max-w-3xl" : "sm:max-w-lg")}>
        <ExpandButton expanded={expanded} onToggle={() => setExpanded((v) => !v)} />
        <SheetHeader><SheetTitle>Edit campaign</SheetTitle></SheetHeader>
        <div className="flex flex-col gap-4 p-4 pt-0">
          {loading ? <p className="text-sm text-muted-foreground">Loading config…</p> : (
            <div className="grid gap-3">
              <div className="grid gap-1.5">
                <label className="text-xs font-medium">Campaign name *</label>
                <Input value={name} onChange={(e) => setName(e.target.value)} />
              </div>
              <div className="grid gap-1.5">
                <label className="text-xs font-medium">What you sell / offer *</label>
                <Textarea rows={2} value={offer} onChange={(e) => setOffer(e.target.value)} />
              </div>
              <div className="grid gap-1.5">
                <label className="text-xs font-medium">Target industries <span className="text-muted-foreground font-normal">(comma-separated)</span></label>
                <Input value={industries} onChange={(e) => setIndustries(e.target.value)} />
              </div>
              <div className="grid gap-1.5">
                <label className="text-xs font-medium">Cities <span className="text-muted-foreground font-normal">(comma-separated)</span></label>
                <Input value={cities} onChange={(e) => setCities(e.target.value)} />
              </div>
              <div className="grid gap-1.5">
                <label className="text-xs font-medium">Areas <span className="text-muted-foreground font-normal">(comma-separated, e.g. G-13, F-11 Markaz)</span></label>
                <Input value={areas} onChange={(e) => setAreas(e.target.value)} />
              </div>
              <div className="grid gap-1.5">
                <label className="text-xs font-medium">Buyer keywords <span className="text-muted-foreground font-normal">(comma-separated)</span></label>
                <Input value={keywords} onChange={(e) => setKeywords(e.target.value)} />
              </div>
              <div className="grid gap-1.5">
                <label className="text-xs font-medium">OSM categories <span className="text-muted-foreground font-normal">(comma-separated)</span></label>
                <Input value={categories} onChange={(e) => setCategories(e.target.value)} />
              </div>
              <div className="grid gap-1.5">
                <label className="text-xs font-medium">Search queries <span className="text-muted-foreground font-normal">(comma-separated)</span></label>
                <Input value={queries} onChange={(e) => setQueries(e.target.value)} />
              </div>
              <div className="flex items-center gap-4">
                <div className="grid gap-1.5">
                  <label className="text-xs font-medium">Max companies</label>
                  <Input type="number" min={1} max={500} value={maxCo} onChange={(e) => setMaxCo(e.target.value)} className="w-24" />
                </div>
                <div className="grid gap-1.5">
                  <label className="text-xs font-medium">Min score</label>
                  <Input type="number" min={0} max={100} value={minScore} onChange={(e) => setMinScore(e.target.value)} className="w-24" />
                </div>
              </div>
            </div>
          )}
          {error && <p className="text-sm text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose} disabled={busy}>Cancel</Button>
            <Button onClick={save} disabled={busy || loading}>{busy ? "Saving…" : "Save"}</Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  )
}

function arr(v: unknown): string[] {
  return Array.isArray(v) ? v.map(String) : []
}

function split(s: string): string[] {
  return s.split(",").map((t) => t.trim()).filter(Boolean)
}

// A small YAML reader matched to toYaml's output: nested maps, block and inline lists, and
// scalars, at ARBITRARY depth. An indentation stack (not a single "current key") is what lets
// it read 3+ levels; the old two-level reader silently flattened anything deeper, so a config
// that toYaml had emitted could not be read back.
function parseYaml(text: string): Record<string, unknown> {
  const root: Record<string, unknown> = {}
  // Each frame owns the keys written at `indent`; the innermost frame is the current map.
  const stack: { indent: number; map: Record<string, unknown> }[] = [{ indent: -1, map: root }]
  // A bare `key:` whose shape (map, list, or empty) is only known once the next line is seen.
  let pending: { key: string; parent: Record<string, unknown>; indent: number } | null = null
  let listTarget: unknown[] | null = null
  let listIndent = -1

  // Strip a trailing "# comment" from an unquoted scalar (a hand-edit; toYaml quotes any value
  // containing '#', so a quoted/bracketed value is left untouched and keeps its literal '#').
  const stripComment = (s: string): string => {
    if (/^['"[{]/.test(s)) return s
    const i = s.indexOf(" #")
    return i >= 0 ? s.slice(0, i).trimEnd() : s
  }

  const parseVal = (s: string): unknown => {
    if (s.startsWith("[") && s.endsWith("]"))
      return s.slice(1, -1).split(",").map((t) => t.trim().replace(/^['"]|['"]$/g, "")).filter(Boolean)
    const n = Number(s)
    if (!isNaN(n) && s.trim() !== "") return n
    if (s === "true") return true
    if (s === "false") return false
    return s.replace(/^['"]|['"]$/g, "")
  }

  for (const line of text.split("\n")) {
    const raw = line.trimEnd()
    if (!raw || raw.trimStart().startsWith("#")) continue
    const indent = raw.length - raw.trimStart().length
    const content = raw.trimStart()
    const isListItem = content.startsWith("- ")

    // Resolve a pending `key:` now that the following line reveals its shape.
    if (pending) {
      if (indent > pending.indent && isListItem) {
        const arr: unknown[] = []
        pending.parent[pending.key] = arr
        listTarget = arr
        listIndent = indent
      } else if (indent > pending.indent) {
        const child: Record<string, unknown> = {}
        pending.parent[pending.key] = child
        stack.push({ indent: pending.indent, map: child })
      } else {
        // No children followed: a bare `key:` means an empty list (toYaml never emits this; it
        // only arises from hand-editing). Matches the previous reader's default.
        pending.parent[pending.key] = []
      }
      pending = null
    }

    if (isListItem) {
      if (listTarget && indent >= listIndent) {
        listTarget.push(content.slice(2).trim().replace(/^['"]|['"]$/g, ""))
      }
      continue
    }

    const kvMatch = content.match(/^(\w[\w-]*):\s*(.*)$/)
    if (!kvMatch) continue
    const [, k, rawV] = kvMatch
    const v = stripComment(rawV)
    listTarget = null

    // Drop back to the container whose children live at this indent.
    while (stack.length > 1 && indent <= stack[stack.length - 1].indent) stack.pop()
    const parent = stack[stack.length - 1].map

    if (v === "") {
      pending = { key: k, parent, indent }
    } else if (v === "[]") {
      parent[k] = []
    } else if (v === "{}") {
      parent[k] = {}
    } else {
      parent[k] = parseVal(v)
    }
  }
  // File ended on a bare `key:` with nothing under it.
  if (pending) pending.parent[pending.key] = []
  return root
}

function toYaml(obj: Record<string, unknown>, indent = 0): string {
  const pad = "  ".repeat(indent)
  const itemPad = "  ".repeat(indent + 1)
  const lines: string[] = []
  for (const [k, v] of Object.entries(obj)) {
    if (v === null || v === undefined) continue
    if (Array.isArray(v)) {
      if (v.length === 0) { lines.push(`${pad}${k}: []`); continue }
      lines.push(`${pad}${k}:`)
      for (const item of v) lines.push(`${itemPad}- ${yamlVal(item)}`)
    } else if (typeof v === "object") {
      const entries = Object.entries(v as Record<string, unknown>).filter(([, val]) => val !== null && val !== undefined)
      if (entries.length === 0) { lines.push(`${pad}${k}: {}`); continue }
      lines.push(`${pad}${k}:`)
      lines.push(toYaml(v as Record<string, unknown>, indent + 1))
    } else {
      lines.push(`${pad}${k}: ${yamlVal(v)}`)
    }
  }
  return lines.join("\n")
}

function yamlVal(v: unknown): string {
  if (typeof v === "string") {
    if (/[:#{}[\],&*?|>!%@`]/.test(v) || v === "" || v === "true" || v === "false") return `'${v.replace(/'/g, "''")}'`
    return v
  }
  return String(v)
}

function RunPanel({ campaign, onFinished }: { campaign: Campaign; onFinished: () => void }) {
  const { keyCount } = useCampaign()
  const [max, setMax] = React.useState(String(campaign.max_companies))
  const [progress, setProgress] = React.useState<RunProgress | null>(campaign.live)
  const [error, setError] = React.useState<string | null>(null)
  const running = progress && !["idle", "completed", "failed"].includes(progress.stage)
  const noKeys = keyCount === 0

  // Adopt the server's live status whenever the campaign list refreshes (e.g. after returning
  // to the page) – unless a local poll is already tracking an active run, so finer-grained
  // local progress is never clobbered by a slightly older list snapshot. This is what stops a
  // dispatched/running campaign from rendering as "nothing ran" after navigation.
  React.useEffect(() => {
    setProgress((cur) => {
      if (cur && !["idle", "completed", "failed"].includes(cur.stage)) return cur
      return campaign.live ?? cur
    })
  }, [campaign.live])

  React.useEffect(() => {
    if (!running) return
    const t = setInterval(async () => {
      try {
        const p = await api.progress(campaign.campaign_id)
        setProgress(p)
        if (p.stage === "completed" || p.stage === "failed") onFinished()
      } catch { /* keep polling */ }
    }, 1500)
    return () => clearInterval(t)
  }, [running, campaign.campaign_id, onFinished])

  const start = async () => {
    setError(null)
    try {
      setProgress(await api.runCampaign(campaign.campaign_id, Number(max) || undefined))
      // Refresh the shared list so campaign.live persists across navigation and the app-level
      // poll picks up the active run immediately.
      onFinished()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const pct = progress && progress.total ? Math.round((progress.done / progress.total) * 100) : 0
  const stats = progress?.stats

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Input className="w-20 h-8 text-sm" value={max} onChange={(e) => setMax(e.target.value)} disabled={!!running} />
        <Button size="sm" onClick={start} disabled={!!running || noKeys} title={noKeys ? "Add at least one API key in Settings first" : undefined}>
          <Play className="size-3.5" /> {running ? "Running…" : "Run"}
        </Button>
        <Button size="sm" variant="outline" onClick={() => api.downloadExport(campaign.campaign_id, { min_score: campaign.min_score })}>
          <Download className="size-3.5" /> CSV
        </Button>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {progress && progress.stage !== "idle" && (
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2 text-xs">
            <Badge variant={progress.stage === "failed" ? "destructive" : progress.stage === "completed" ? "default" : "secondary"} className="text-[10px] px-1.5 py-0">
              {progress.stage}
            </Badge>
            <span className="text-muted-foreground">
              {progress.stage === "discover" ? `${progress.done} found` : progress.total ? `${progress.done}/${progress.total}` : ""} {progress.message}
            </span>
          </div>
          {progress.stage === "process" && <Progress value={pct} className="h-1.5" />}
          {stats && (
            <p className="text-[11px] text-muted-foreground leading-relaxed">
              {stats.discovered} discovered → {stats.after_dedupe} unique · {stats.buyer} buyers · {stats.qualified} qualified · {stats.outreach_ready} outreach-ready
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function StatCell({ label, value, highlight }: { label: string; value: number; highlight?: boolean }) {
  return (
    <div className={cn("px-4 py-2.5 text-center", highlight && "bg-brand-muted/40")}>
      <div className="text-lg font-semibold leading-none tabular-nums">{value}</div>
      <div className="mt-1 text-[11px] text-muted-foreground">{label}</div>
    </div>
  )
}

function statusDotClass(status?: string): string {
  const s = (status ?? "").toLowerCase()
  if (["completed", "done", "success", "succeeded"].includes(s)) return "bg-green-500"
  if (["running", "dispatched", "pending", "queued"].includes(s)) return "bg-amber-500"
  if (["failed", "error"].includes(s)) return "bg-red-500"
  return "bg-muted-foreground/40"
}

export default function CampaignsPage() {
  const { campaigns, setCampaignId, refresh, loading, error } = useCampaign()
  const router = useRouter()
  const [creating, setCreating] = React.useState(false)
  const [editing, setEditing] = React.useState<Campaign | null>(null)
  const onFinished = React.useCallback(() => { refresh(true) }, [refresh])

  const openLeads = React.useCallback((id: string) => {
    setCampaignId(id)
    router.push("/leads")
  }, [setCampaignId, router])

  const onCreated = React.useCallback(async (campaignId: string) => {
    await refresh(true)
    setCampaignId(campaignId)
  }, [refresh, setCampaignId])

  // Always get fresh run status + counts when landing on this page, so a run dispatched
  // earlier (now in progress or finished on GitHub Actions) is reflected instead of a stale
  // "nothing ran" snapshot from a cached list.
  React.useEffect(() => { void refresh(true) }, [refresh])

  const remove = async (c: Campaign) => {
    if (!confirm(`Delete campaign "${c.name}"? Leads already generated are kept.`)) return
    try { await api.deleteCampaign(c.campaign_id); await refresh(true) } catch (e) { alert((e as Error).message) }
  }

  return (
    <div className="mx-auto grid max-w-7xl gap-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Campaigns</h1>
          <p className="mt-1 text-sm text-muted-foreground">Create a search, run discovery, and results land in Leads.</p>
        </div>
        <Button onClick={() => setCreating(true)}><Plus data-icon="inline-start" /> New campaign</Button>
      </div>
      <NewCampaign open={creating} onClose={() => setCreating(false)} onCreated={onCreated} />
      {editing && <EditCampaign campaign={editing} open={true} onClose={() => setEditing(null)} onSaved={() => { refresh(true); setEditing(null) }} />}
      {error && <p className="text-sm text-destructive">API error: {error}</p>}
      {loading && <p className="text-muted-foreground">Loading…</p>}
      {!loading && campaigns.length === 0 && (
        <Card>
          <CardContent className="flex flex-col items-center py-14 text-center">
            <div className="flex size-12 items-center justify-center rounded-xl bg-muted text-muted-foreground">
              <Plus className="size-6" />
            </div>
            <h2 className="mt-5 text-lg font-semibold">No campaigns yet</h2>
            <p className="mt-2 max-w-sm text-sm text-muted-foreground">Describe what you sell and Qualifyr finds the companies that need it.</p>
            <Button className="mt-6" onClick={() => setCreating(true)}><Plus data-icon="inline-start" /> New campaign</Button>
          </CardContent>
        </Card>
      )}
      {campaigns.map((c) => {
        const offer = typeof c.offer === "string" ? c.offer : JSON.stringify(c.offer)
        const showOffer = offer !== c.name

        return (
          <Card
            key={c.campaign_id}
            className="overflow-hidden cursor-pointer select-none transition-all hover:border-primary/30 hover:shadow-md"
            onClick={() => openLeads(c.campaign_id)}
            title="View leads for this campaign"
          >
            <CardContent className="flex flex-col gap-4 p-5">
              {/* Header row */}
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1 overflow-hidden">
                  <h3 className="truncate font-semibold tracking-tight">{c.name}</h3>
                  {showOffer && <p className="mt-0.5 line-clamp-1 text-sm text-muted-foreground">{offer}</p>}
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  {(() => {
                    const pills = [...c.cities, ...(c.areas ?? [])]
                    const MAX_PILLS = 4
                    const shown = pills.slice(0, MAX_PILLS)
                    const extra = pills.length - MAX_PILLS
                    return (
                      <div className="hidden flex-wrap items-center gap-1 sm:flex">
                        {shown.map((p) => <Badge key={p} variant="outline" className="px-1.5 py-0 text-[11px]">{p}</Badge>)}
                        {extra > 0 && <Badge variant="outline" className="px-1.5 py-0 text-[11px]">+{extra}</Badge>}
                      </div>
                    )
                  })()}
                  <div className="flex items-center" onClick={(e) => e.stopPropagation()}>
                    <Button variant="ghost" size="icon" className="size-7 text-muted-foreground hover:text-primary" onClick={() => setEditing(c)}>
                      <Pencil className="size-3.5" />
                    </Button>
                    <Button variant="ghost" size="icon" className="size-7 text-muted-foreground hover:text-destructive" onClick={() => remove(c)}>
                      <Trash2 className="size-3.5" />
                    </Button>
                  </div>
                </div>
              </div>

              {/* Body: stat strip + run controls */}
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div className="grid grid-cols-4 divide-x divide-border/60 overflow-hidden rounded-lg border border-border/60 bg-muted/20">
                  <StatCell label="Companies" value={c.leads} />
                  <StatCell label="Buyers" value={c.buyers} />
                  <StatCell label="Qualified" value={c.qualified} highlight />
                  <StatCell label="Outreach" value={c.outreach_ready} />
                </div>
                <div onClick={(e) => e.stopPropagation()}>
                  <RunPanel campaign={c} onFinished={onFinished} />
                </div>
              </div>

              {/* Last run – compact meta with a status dot */}
              <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                <span className={cn("size-1.5 rounded-full", statusDotClass(c.last_run?.status))} />
                {c.last_run
                  ? `Last run ${c.last_run.status} · ${new Date(c.last_run.started_at).toLocaleDateString()}`
                  : "Not run yet"}
              </div>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}
