//! Maud templates. Renders HTML for full pages and HTMX partials.

use maud::{html, Markup, PreEscaped, DOCTYPE};

use crate::models::{DisplayRow, SqueueRow, TreeNode, Usage};

const TAILWIND_CDN: &str = "https://cdn.tailwindcss.com";
const HTMX_CDN: &str = "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js";
const HTMX_SSE_CDN: &str = "https://unpkg.com/htmx-ext-sse@2.2.2/sse.js";
const ALPINE_CDN: &str = "https://unpkg.com/alpinejs@3.14.3/dist/cdn.min.js";

/// Base HTML layout shared by all pages.
pub fn layout(title: &str, content: Markup) -> Markup {
    html! {
        (DOCTYPE)
        html lang="en" class="h-full" {
            head {
                meta charset="UTF-8";
                meta name="viewport" content="width=device-width, initial-scale=1.0";
                title { (title) }
                script src=(TAILWIND_CDN) {}
                script src=(HTMX_CDN) {}
                script src=(HTMX_SSE_CDN) {}
                script defer src=(ALPINE_CDN) {}
                script {
                    (PreEscaped(r#"
                    tailwind.config = {
                      theme: { extend: {
                        colors: { gray: { 950: '#0a0c10' } },
                        fontFamily: {
                          mono: ['JetBrains Mono', 'Cascadia Code', 'Fira Code', 'ui-monospace', 'monospace'],
                        }
                      } }
                    }
                    "#))
                }
                style {
                    (PreEscaped(r#"
                    body { background-color: #0a0c10; }
                    .htmx-indicator { display: none; }
                    .htmx-request .htmx-indicator { display: inline-flex; }
                    .htmx-request.htmx-indicator { display: inline-flex; }
                    tr.htmx-swapping { opacity: 0; transition: opacity 0.2s ease-out; }
                    ::-webkit-scrollbar { width: 6px; height: 6px; }
                    ::-webkit-scrollbar-track { background: #1a1d24; }
                    ::-webkit-scrollbar-thumb { background: #374151; border-radius: 3px; }
                    ::-webkit-scrollbar-thumb:hover { background: #4b5563; }
                    "#))
                }
            }
            body class="h-full text-gray-100 font-sans antialiased" {
                nav class="border-b border-gray-800 bg-gray-950/80 backdrop-blur sticky top-0 z-30" {
                    div class="max-w-screen-2xl mx-auto px-4 h-12 flex items-center gap-3" {
                        span class="text-indigo-400 font-mono font-bold tracking-tight" { "slurm-dash" }
                        span class="text-gray-600 text-xs" { "rust · web" }
                        div class="flex-1" {}
                        span id="global-spinner" class="htmx-indicator text-xs text-gray-500 flex items-center gap-1.5" {
                            svg class="animate-spin h-3.5 w-3.5 text-indigo-400" fill="none" viewBox="0 0 24 24" {
                                circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" {}
                                path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z" {}
                            }
                            "syncing…"
                        }
                    }
                }
                div id="modal-slot" {}
                main class="max-w-screen-2xl mx-auto px-4 py-6"
                     hx-ext="sse"
                     sse-connect="/events" {
                    (content)
                }
            }
        }
    }
}

pub fn dashboard(aliases: &[String]) -> Markup {
    let content = html! {
        @if aliases.is_empty() {
            div class="flex flex-col items-center justify-center py-32 text-gray-500" {
                p class="text-lg mb-2" { "No servers configured." }
                p class="text-sm font-mono" { "slurm-dash add user@cluster --alias mycluster" }
            }
        } @else if aliases.len() == 1 {
            (server_panel(&aliases[0], true))
        } @else {
            div x-data=(format!("{{ tab: '{}' }}", aliases[0])) {
                div class="flex gap-1 border-b border-gray-800 mb-4" {
                    @for alias in aliases {
                        button
                            "@click"=(format!("tab = '{alias}'"))
                            ":class"=(format!("tab === '{alias}' ? 'border-b-2 border-indigo-500 text-indigo-300' : 'text-gray-500 hover:text-gray-300'"))
                            class="px-4 py-2 text-sm font-mono transition-colors -mb-px" {
                            (alias)
                        }
                    }
                }
                @for alias in aliases {
                    div x-show=(format!("tab === '{alias}'")) {
                        (server_panel(alias, true))
                    }
                }
            }
        }
    };
    layout("slurm-dash", content)
}

/// A wrapper that lazy-loads the jobs table. The wrapper subscribes to SSE
/// events for the alias and re-fetches on update.
pub fn server_panel(alias: &str, with_initial_load: bool) -> Markup {
    let trigger = if with_initial_load {
        format!("load, sse:jobs-{alias}")
    } else {
        format!("sse:jobs-{alias}")
    };
    html! {
        div id=(format!("server-{alias}"))
            hx-get=(format!("/jobs/{alias}"))
            hx-trigger=(trigger)
            hx-swap="innerHTML" {
            div class="py-12 text-center text-gray-600 text-xs" { "Loading…" }
        }
    }
}

pub fn jobs_table(
    alias: &str,
    rows: &[DisplayRow],
    usage: &Usage,
    tags: &[String],
    active_tag: Option<&str>,
) -> Markup {
    html! {
        div id=(format!("jobs-table-{alias}"))
             x-data="{ selectedRows: new Set(), lastChecked: null }" {
            (usage_bar(alias, usage))
            (tag_filter_bar_inner(alias, tags, active_tag))
            @if rows.is_empty() {
                div class="rounded-xl border border-gray-800 py-16 text-center text-gray-600" {
                    p { "No jobs tracked yet." }
                    p class="text-xs mt-1" { "Submit a job on the cluster — the wrapper will capture it automatically." }
                }
            } @else {
                div class="rounded-xl border border-gray-800 overflow-hidden" {
                    table class="w-full text-xs" {
                        thead {
                            tr class="bg-gray-900 border-b border-gray-800 text-gray-500 uppercase tracking-wider text-[10px]" {
                                th class="px-3 py-2.5 text-center font-medium w-8" {
                                    input type="checkbox"
                                          "@change"=(format!("if($event.target.checked) {{ document.querySelectorAll('.job-row-{alias} input[type=checkbox]').forEach(cb => {{ cb.checked = true; cb.dispatchEvent(new Event('change')) }}); }} else {{ selectedRows.clear(); document.querySelectorAll('.job-row-{alias} input[type=checkbox]').forEach(cb => cb.checked = false); }}"))
                                          class="rounded border-gray-600 bg-gray-800 text-indigo-500 focus:ring-0 w-4 h-4";
                                }
                                th class="px-3 py-2.5 text-left font-medium" { "Job ID" }
                                th class="px-3 py-2.5 text-left font-medium" { "Job Name" }
                                th class="px-3 py-2.5 text-left font-medium" { "State" }
                                th class="px-3 py-2.5 text-left font-medium" { "Submitted" }
                                th class="px-3 py-2.5 text-left font-medium max-w-xs" { "Work Dir" }
                                th class="px-3 py-2.5 text-left font-medium" { "CPUs" }
                                th class="px-3 py-2.5 text-left font-medium" { "Req Mem" }
                                th class="px-3 py-2.5 text-left font-medium" { "GPUs" }
                                th class="px-3 py-2.5 text-left font-medium" { "Nodes" }
                                th class="px-3 py-2.5 text-left font-medium" { "Git" }
                                th class="px-3 py-2.5 text-left font-medium" { "Tags" }
                                th class="px-3 py-2.5 text-right font-medium" { "Actions" }
                            }
                        }
                        tbody class="divide-y divide-gray-800/60" {
                            @for row in rows {
                                (job_row(alias, row))
                            }
                        }
                    }
                }
            }
        }
    }
}

fn usage_bar(alias: &str, usage: &Usage) -> Markup {
    html! {
        div class="flex items-center gap-3 mb-3 text-sm" {
            button
                hx-get=(format!("/squeue/{alias}"))
                hx-target="#modal-slot"
                hx-swap="innerHTML"
                class="flex items-center gap-3 px-3 py-1.5 rounded-lg bg-gray-900 hover:bg-gray-800 border border-gray-800 hover:border-gray-700 transition-colors cursor-pointer" {
                @if usage.running > 0 {
                    span class="text-green-400 font-medium" { (usage.running) " running" }
                    span class="text-gray-600" { "·" }
                    span class="text-gray-400" { (usage.cpus) " CPUs" }
                    @if usage.gpus > 0 {
                        span class="text-gray-600" { "·" }
                        span class="text-gray-400" { (usage.gpus) " GPUs" }
                    }
                    @if usage.mem_gb > 0.0 {
                        span class="text-gray-600" { "·" }
                        span class="text-gray-400" { (usage.mem_gb) " GB" }
                    }
                    span class="text-gray-600" { "·" }
                }
                @if usage.pending > 0 {
                    span class="text-yellow-400" { (usage.pending) " pending" }
                    span class="text-gray-600" { "·" }
                }
                span class="text-gray-500" { (usage.total) " known" }
            }
            button
                hx-post=(format!("/sync/{alias}"))
                hx-target=(format!("#jobs-table-{alias}"))
                hx-swap="outerHTML"
                hx-indicator="#global-spinner"
                class="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-900 hover:bg-gray-800 border border-gray-800 hover:border-gray-700 text-gray-400 hover:text-gray-200 transition-colors text-xs" {
                "Sync"
            }
            button
                "x-show"="selectedRows.size > 0"
                "@click"=(format!("(()=>{{ const tagName = prompt('Tag name:'); if(tagName && tagName.trim()) {{ fetch('/jobs/{alias}/tag-multiple', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{job_ids:[...selectedRows], tag_name:tagName}})}}).then(() => htmx.trigger('#jobs-table-{alias}', 'sse:jobs-{alias}')); }} }})()"))
                class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-indigo-950 hover:bg-indigo-900 border border-indigo-900 hover:border-indigo-800 text-indigo-400 hover:text-indigo-200 transition-colors text-xs" {
                "Tag " span "x-text"="selectedRows.size" {}
            }
            button
                "x-show"="selectedRows.size > 0"
                "@click"=(format!("if(selectedRows.size > 0 && confirm('Delete '+selectedRows.size+' job(s)?')) {{ fetch('/jobs/{alias}/delete-multiple', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{job_ids:[...selectedRows]}})}}).then(() => htmx.trigger('#jobs-table-{alias}', 'sse:jobs-{alias}')); }}"))
                class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-950 hover:bg-red-900 border border-red-900 hover:border-red-800 text-red-400 hover:text-red-200 transition-colors text-xs" {
                "Delete " span "x-text"="selectedRows.size" {}
            }
        }
    }
}

fn tag_filter_bar_inner(alias: &str, tags: &[String], active_tag: Option<&str>) -> Markup {
    html! {
        div class="flex gap-1.5 flex-wrap mb-2" {
            span class="text-xs text-gray-500 self-center mr-1" { "Tags:" }
            @if tags.is_empty() {
                span class="text-xs text-gray-600 italic" { "No tags yet" }
            } @else {
                @for tag in tags {
                    @let is_active = active_tag == Some(tag.as_str());
                    @let url = if is_active { format!("/jobs/{alias}") } else { format!("/jobs/{alias}?tag={}", urlencoding::encode(tag)) };
                    button
                        hx-get=(url)
                        hx-target=(format!("#jobs-table-{alias}"))
                        hx-swap="outerHTML"
                        class=(if is_active {
                            "px-2 py-0.5 rounded text-[10px] font-medium bg-indigo-600 text-indigo-50 border border-indigo-500 transition-colors"
                        } else {
                            "px-2 py-0.5 rounded text-[10px] font-medium bg-indigo-950 text-indigo-300 hover:bg-indigo-900 border border-indigo-900 transition-colors"
                        }) {
                        @if is_active { "✓ " (tag) " (clear)" } @else { (tag) }
                    }
                }
            }
        }
    }
}

fn job_row(alias: &str, row: &DisplayRow) -> Markup {
    html! {
        tr id=(format!("row-{alias}-{}", row.job_id))
           class=(format!("job-row-{alias} hover:bg-gray-900/60 transition-colors group cursor-pointer"))
           hx-get=(format!("/jobs/{alias}/{}/files", row.job_id))
           hx-target="#modal-slot"
           hx-swap="innerHTML" {
            td class="px-3 py-2 text-center" onclick="event.stopPropagation()" {
                input type="checkbox"
                      "@click"=(format!("(() => {{ const el=$event.target; if(el.shiftKey && $data.lastChecked) {{ const all=[...document.querySelectorAll('.job-row-{alias} input[type=checkbox]')]; const i1=all.indexOf($data.lastChecked); const i2=all.indexOf(el); const [lo,hi]=[Math.min(i1,i2),Math.max(i1,i2)]; all.slice(lo,hi+1).forEach(b => {{ b.checked=el.checked; b.dispatchEvent(new Event('change')); }}); }} $data.lastChecked=el; }})()"))
                      "@change"=(format!("if($event.target.checked) {{ selectedRows.add('{}'); }} else {{ selectedRows.delete('{}'); }}", row.job_id, row.job_id))
                      onclick="event.stopPropagation()"
                      class="rounded border-gray-600 bg-gray-800 text-indigo-500 focus:ring-0 w-4 h-4 cursor-pointer";
            }
            td class="px-3 py-2 font-mono text-indigo-300 cursor-pointer" { (row.job_id) }
            td class="px-3 py-2 text-gray-400" { (row.job_name) }
            td class="px-3 py-2" {
                @if !row.state.is_empty() {
                    span class=(format!("inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium {}", row.state_class)) { (row.state) }
                }
            }
            td class="px-3 py-2 text-gray-400 whitespace-nowrap" { (row.submit_time) }
            td class="px-3 py-2 font-mono text-gray-400 max-w-xs truncate" title=(row.work_dir) { (row.work_dir) }
            td class="px-3 py-2 text-gray-400" { (row.cpus) }
            td class="px-3 py-2 text-gray-400" { (row.req_mem) }
            td class="px-3 py-2 text-gray-400" {
                @if row.gpus > 0 {
                    (row.gpus)
                    @if !row.gpu_model.is_empty() {
                        " " span class="text-gray-500" { "(" (row.gpu_model) ")" }
                    }
                }
            }
            td class="px-3 py-2 font-mono text-gray-500 truncate max-w-[8rem]" title=(row.node_list) { (row.node_list) }
            td class="px-3 py-2 font-mono text-gray-600" { (row.git_hash) }
            td class="px-3 py-2" {
                @for tag in &row.tags {
                    span class="inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium bg-indigo-950 text-indigo-300 mr-1 mb-0.5" { (tag) }
                }
            }
            td class="px-3 py-2 text-right" onclick="event.stopPropagation()" {
                button
                    hx-delete=(format!("/jobs/{alias}/{}", row.job_id))
                    hx-target=(format!("#row-{alias}-{}", row.job_id))
                    hx-swap="outerHTML swap:0.25s"
                    hx-confirm=(format!("Delete job {}?", row.job_id))
                    class="opacity-0 group-hover:opacity-100 p-1 rounded text-gray-600 hover:text-red-400 hover:bg-red-950/50 transition-all"
                    title="Delete" {
                    "✕"
                }
            }
        }
    }
}

pub fn squeue_modal(
    alias: &str,
    rows: &[SqueueRow],
    error: Option<&str>,
    running: u64,
    pending: u64,
    total: u64,
) -> Markup {
    html! {
        div class="fixed inset-0 z-50 flex items-center justify-center p-4"
            "@keydown.escape.window"="$el.remove()"
            x-data="{}" {
            div class="absolute inset-0 bg-black/70 backdrop-blur-sm"
                "@click"="$el.closest('[x-data]').remove()" {}
            div class="relative w-full max-w-4xl max-h-[84vh] flex flex-col rounded-2xl border border-gray-700 bg-gray-950 shadow-2xl overflow-hidden" {
                div class="flex items-center gap-3 px-5 py-4 border-b border-gray-800" {
                    span class="text-xs font-mono text-gray-300" { "squeue --me" }
                    span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-indigo-950 text-indigo-400 border border-indigo-900" { (alias) }
                    div class="flex-1" {}
                    button
                        hx-get=(format!("/squeue/{alias}"))
                        hx-target="#modal-slot"
                        hx-swap="innerHTML"
                        hx-indicator="#global-spinner"
                        class="flex items-center gap-1.5 px-3 py-1 rounded-lg bg-gray-900 hover:bg-gray-800 border border-gray-800 text-gray-400 hover:text-gray-200 text-xs transition-colors" {
                        "Refresh"
                    }
                    button "@click"="$el.closest('[x-data]').remove()"
                           class="p-1.5 rounded-lg text-gray-500 hover:text-gray-200 hover:bg-gray-800 transition-colors" { "✕" }
                }
                div class="flex items-center gap-4 px-5 py-2.5 border-b border-gray-800 text-xs" {
                    @if running > 0 { span class="text-green-400 font-medium" { (running) " running" } }
                    @if pending > 0 { span class="text-yellow-400" { (pending) " pending" } }
                    span class="text-gray-600" { (total) " total" }
                }
                @if let Some(err) = error {
                    div class="p-5 text-sm text-red-400" { (err) }
                } @else if rows.is_empty() {
                    div class="flex items-center justify-center py-12 text-gray-600 text-sm" { "No jobs in the queue." }
                } @else {
                    div class="flex-1 overflow-auto" {
                        table class="w-full text-xs" {
                            thead class="sticky top-0" {
                                tr class="bg-gray-900 border-b border-gray-800 text-gray-500 uppercase tracking-wider text-[10px]" {
                                    th class="px-3 py-2.5 text-left font-medium" { "Job ID" }
                                    th class="px-3 py-2.5 text-left font-medium" { "State" }
                                    th class="px-3 py-2.5 text-left font-medium" { "Partition" }
                                    th class="px-3 py-2.5 text-left font-medium" { "Name" }
                                    th class="px-3 py-2.5 text-left font-medium" { "CPUs" }
                                    th class="px-3 py-2.5 text-left font-medium" { "GPUs" }
                                    th class="px-3 py-2.5 text-left font-medium" { "Node / Reason" }
                                    th class="px-3 py-2.5 text-left font-medium" { "Time" }
                                    th class="px-3 py-2.5 text-left font-medium w-40" { "Progress" }
                                }
                            }
                            tbody class="divide-y divide-gray-800/60" {
                                @for j in rows {
                                    tr class="hover:bg-gray-900/60 transition-colors" {
                                        td class="px-3 py-2 font-mono text-indigo-300" { (j.job_id) }
                                        td class="px-3 py-2" {
                                            span class=(format!("inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium {}", j.state_class)) { (j.state) }
                                        }
                                        td class="px-3 py-2 text-gray-400" { (j.partition) }
                                        td class="px-3 py-2 text-gray-300 max-w-[10rem] truncate font-mono" title=(j.name) { (j.name) }
                                        td class="px-3 py-2 text-gray-400" { (j.cpus) }
                                        td class="px-3 py-2 text-gray-400" { @if j.gpus > 0 { (j.gpus) } }
                                        td class="px-3 py-2 font-mono text-gray-500 max-w-[8rem] truncate" title=(j.node_reason) { (j.node_reason) }
                                        td class="px-3 py-2 text-gray-400 whitespace-nowrap" {
                                            (j.time_used)
                                            @if !j.time_limit.is_empty() && !matches!(j.time_limit.as_str(), "UNLIMITED" | "NOT_SET" | "N/A") {
                                                span class="text-gray-600" { " / " (j.time_limit) }
                                            }
                                        }
                                        td class="px-3 py-2" {
                                            @if let Some(p) = &j.progress {
                                                div class="flex items-center gap-2" {
                                                    div class="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden" {
                                                        div class=(format!("h-full rounded-full {}", p.color))
                                                            style=(format!("width: {}%", p.pct)) {}
                                                    }
                                                    span class="text-[10px] text-gray-500 whitespace-nowrap" { (p.pct) "%" }
                                                }
                                                div class="text-[10px] text-gray-600 mt-0.5" { (p.remaining) " left" }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

pub fn files_modal(
    alias: &str,
    job_id: &str,
    submit_cmd: &str,
    work_dir: &str,
) -> Markup {
    html! {
        div
            x-data=(format!(r#"{{
                tab: 'snapshot',
                envExpand: false,
                activeFile: '',
                loadPreview(path) {{
                    if (!path) return;
                    const url = '/jobs/{alias}/{job_id}/snapshot/file?path=' + encodeURIComponent(path) + '&expand_env=' + (this.envExpand ? 1 : 0);
                    htmx.ajax('GET', url, {{ target: '#snapshot-preview-{alias}-{job_id}', swap: 'innerHTML' }});
                }}
            }}"#))
            class="fixed inset-0 z-50 flex items-center justify-center p-4"
            "@keydown.escape.window"="$el.remove()" {
            div class="absolute inset-0 bg-black/70 backdrop-blur-sm" "@click"="$el.closest('[x-data]').remove()" {}
            div class="relative w-full max-w-5xl max-h-[88vh] flex flex-col rounded-2xl border border-gray-700 bg-gray-950 shadow-2xl overflow-hidden" {
                div class="flex items-start gap-3 px-5 py-4 border-b border-gray-800" {
                    div class="flex-1 min-w-0" {
                        div class="flex items-center gap-2 mb-1" {
                            span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-indigo-950 text-indigo-400 border border-indigo-900" { (alias) }
                            span class="text-gray-300 font-mono font-medium" { "job " (job_id) }
                        }
                        @if !submit_cmd.is_empty() {
                            p class="text-xs font-mono text-gray-500 truncate" title=(submit_cmd) { (submit_cmd) }
                        }
                        @if !work_dir.is_empty() {
                            p class="text-xs font-mono text-gray-600 truncate" { (work_dir) }
                        }
                    }
                    button "@click"="$el.closest('[x-data]').remove()"
                           class="flex-shrink-0 p-1.5 rounded-lg text-gray-500 hover:text-gray-200 hover:bg-gray-800 transition-colors" { "✕" }
                }
                div class="flex gap-0 border-b border-gray-800 px-5" {
                    button "@click"="tab = 'snapshot'"
                           ":class"="tab === 'snapshot' ? 'border-b-2 border-indigo-500 text-indigo-300' : 'text-gray-500 hover:text-gray-300'"
                           class="px-3 py-2.5 text-xs font-medium transition-colors -mb-px" { "Submit Snapshot" }
                }
                div "x-show"="tab === 'snapshot'" class="flex-1 flex overflow-hidden min-h-0" {
                    div class="w-72 flex-shrink-0 border-r border-gray-800 flex flex-col overflow-hidden" {
                        div class="flex items-center justify-between px-3 py-2 border-b border-gray-800" {
                            span class="text-[10px] text-gray-500 uppercase tracking-wider" { "Files" }
                            label class="flex items-center gap-1.5 text-[10px] text-gray-500 cursor-pointer select-none" {
                                input type="checkbox" "x-model"="envExpand"
                                      "@change"="loadPreview(activeFile)"
                                      class="rounded border-gray-700 bg-gray-800 text-indigo-500 focus:ring-0 w-3 h-3";
                                "Expand env"
                            }
                        }
                        div id=(format!("snapshot-files-{alias}-{job_id}"))
                            hx-get=(format!("/jobs/{alias}/{job_id}/snapshot"))
                            hx-trigger="load"
                            hx-swap="innerHTML"
                            class="flex-1 overflow-y-auto" {
                            div class="flex items-center justify-center py-8 text-gray-600 text-xs" { "Loading…" }
                        }
                    }
                    div class="flex-1 flex flex-col overflow-hidden min-w-0" {
                        div class="flex items-center gap-2 px-3 py-2 border-b border-gray-800" {
                            span "x-text"="activeFile || 'No file selected'"
                                 class="text-[10px] font-mono text-gray-500 truncate flex-1" {}
                        }
                        div id=(format!("snapshot-preview-{alias}-{job_id}"))
                            class="flex-1 overflow-auto bg-gray-950" {
                            p class="text-gray-600 text-xs italic p-4" { "Select a file to preview." }
                        }
                    }
                }
            }
        }
        script {
            (PreEscaped(format!(r#"
            (function() {{
              const containerId = 'snapshot-files-{alias}-{job_id}';
              function setupClicks() {{
                const container = document.getElementById(containerId);
                if (!container) return;
                container.querySelectorAll('[data-file-path]').forEach(function(el) {{
                  el.addEventListener('click', function() {{
                    container.querySelectorAll('[data-file-path]').forEach(function(x) {{
                      x.classList.remove('bg-indigo-950', 'text-indigo-300');
                    }});
                    el.classList.add('bg-indigo-950', 'text-indigo-300');
                    const modal = container.closest('[x-data]');
                    const data = Alpine.$data(modal);
                    data.activeFile = el.dataset.filePath;
                    data.loadPreview(el.dataset.filePath);
                  }});
                }});
                const auto = container.querySelector('[data-auto-open]');
                if (auto) auto.click();
              }}
              document.addEventListener('htmx:afterSwap', function(e) {{
                if (e.detail.target && e.detail.target.id === containerId) setupClicks();
              }});
            }})();
            "#)))
        }
    }
}

pub fn snapshot_file_list(
    tree: &[TreeNode],
    error: Option<&str>,
) -> Markup {
    html! {
        @if let Some(err) = error {
            p class="text-xs text-red-400 p-3" { (err) }
        } @else {
            ul class="py-1 space-y-0.5" {
                li data-file-path="submit_script.sh"
                   data-auto-open="true"
                   class="flex items-center gap-1.5 py-0.5 text-xs font-mono cursor-pointer transition-colors text-yellow-400 hover:bg-yellow-950/40 hover:text-yellow-200"
                   style="padding-left: 10px; padding-right: 8px"
                   title="submit_script.sh" {
                    span class="truncate min-w-0" { "◆ submit_script.sh" }
                    span class="ml-auto flex-shrink-0 text-[9px] px-1 py-0.5 rounded bg-yellow-950 text-yellow-600 border border-yellow-900/50 whitespace-nowrap" { "captured" }
                }
                @if !tree.is_empty() {
                    li x-data="{ open: false }" class="select-none" {
                        div "@click"="open = !open"
                            class="flex items-center gap-1.5 py-0.5 text-xs font-mono text-gray-400 hover:text-gray-200 hover:bg-gray-800/40 cursor-pointer transition-colors"
                            style="padding-left: 10px; padding-right: 8px" {
                            span ":class"="open ? 'rotate-90' : ''"
                                 class="w-2.5 inline-block text-gray-600 transition-transform" { "▶" }
                            span class="truncate min-w-0" { "Snapshot" }
                            span class="ml-auto flex-shrink-0 text-[10px] text-gray-700" { (tree.len()) " items" }
                        }
                        ul "x-show"="open" {
                            (render_tree_nodes(tree, 1))
                        }
                    }
                } @else {
                    p class="text-xs text-gray-600 italic p-3" { "No snapshot contents." }
                }
            }
        }
    }
}

fn render_tree_nodes(nodes: &[TreeNode], depth: usize) -> Markup {
    let pad = 10 + depth * 14;
    html! {
        @for node in nodes {
            @if node.is_dir {
                li x-data="{ open: false }" class="select-none" {
                    div "@click"="open = !open"
                        class="flex items-center gap-1.5 py-0.5 text-xs font-mono text-gray-400 hover:text-gray-200 hover:bg-gray-800/40 cursor-pointer transition-colors"
                        style=(format!("padding-left: {pad}px; padding-right: 8px")) {
                        span ":class"="open ? 'rotate-90' : ''"
                             class="w-2.5 inline-block text-gray-600 transition-transform" { "▶" }
                        span class="truncate min-w-0" { (node.name) }
                        @if !node.children.is_empty() {
                            span class="ml-auto flex-shrink-0 text-[10px] text-gray-700" { (node.children.len()) }
                        }
                    }
                    ul "x-show"="open" {
                        (render_tree_nodes(&node.children, depth + 1))
                    }
                }
            } @else {
                li data-file-path=(node.path)
                   class=(if node.is_submit_script {
                       "flex items-center gap-1.5 py-0.5 text-xs font-mono cursor-pointer transition-colors text-yellow-400 hover:bg-yellow-950/40 hover:text-yellow-200"
                   } else {
                       "flex items-center gap-1.5 py-0.5 text-xs font-mono cursor-pointer transition-colors text-gray-400 hover:bg-gray-800/70 hover:text-gray-200"
                   })
                   style=(format!("padding-left: {pad}px; padding-right: 8px"))
                   title=(node.path) {
                    span class="truncate min-w-0" {
                        @if node.is_submit_script { "◆ " }
                        (node.name)
                    }
                }
            }
        }
    }
}

pub fn snapshot_preview(content: &str, error: Option<&str>) -> Markup {
    html! {
        @if let Some(err) = error {
            p class="text-xs text-red-400 p-4" { (err) }
        } @else if content.is_empty() {
            p class="text-xs text-gray-600 italic p-4" { "Empty file." }
        } @else {
            pre class="text-xs font-mono text-gray-300 p-4 whitespace-pre-wrap break-all leading-relaxed" {
                (content)
            }
        }
    }
}
