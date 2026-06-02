<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { Activity, BarChart3, CircleAlert, Clock3, Database, GitBranch, LockKeyhole, LogOut, RefreshCcw, Server } from "lucide-vue-next";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Separator } from "@/components/ui/Separator";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/Table";

type Backend = { name: string; kind: string; status: string; latency_ms: number | null; models_count: number | null; detail?: string };
type PublishedModel = { id: string; owned_by?: string; source?: string; route?: string; visible?: boolean };
type QueueRow = { queue_key: string; active: boolean; active_model?: string; active_seconds?: number; waiting: number; max_queue_size: number; timeout_seconds: number; last_wait_ms?: number; avg_wait_ms?: number; queued_total: number; full_total: number; timeout_total: number };
type RequestRow = { ts: number; path: string; model?: string; status: number; queue_key?: string; queue_position?: number; queue_wait_ms?: number; client?: string; duration_ms?: number; upstream?: string };
type ErrorRow = { ts: number; path: string; error: string };
type LoadBucket = { ts: number; total: number; ok: number; errors: number; queued: number; queue_wait_ms: number };
type Dashboard = {
  summary: { uptime_human: string; total_requests: number; published_models: number; backends_online: number; backends_total: number; last_request_at?: number | null };
  backends: Backend[];
  published_models: PublishedModel[];
  queues: QueueRow[];
  stats: { recent_requests: RequestRow[]; recent_errors: ErrorRow[]; load_buckets: LoadBucket[] };
};

const pin = ref("");
const pinMessage = ref("");
const isLocked = ref(true);
const isLoading = ref(false);
const dashboard = ref<Dashboard | null>(null);
const loadError = ref("");
const refreshTime = ref("");
let timer: number | undefined;

const queues = computed(() => dashboard.value?.queues ?? []);
const buckets = computed(() => dashboard.value?.stats.load_buckets ?? []);
const lastBucket = computed(() => buckets.value[buckets.value.length - 1]);
const activeQueues = computed(() => queues.value.filter((row) => row.active).length);
const waitingRequests = computed(() => sum(queues.value, "waiting"));
const maxBucketTotal = computed(() => Math.max(1, ...buckets.value.slice(-60).map((row) => row.total || 0)));
const summaryCards = computed(() => {
  const s = dashboard.value?.summary;
  if (!s) return [];
  return [
    ["Uptime", s.uptime_human, Clock3],
    ["Total requests", s.total_requests, Activity],
    ["Published models", s.published_models, Database],
    ["Backend online", `${s.backends_online}/${s.backends_total}`, Server],
    ["Last request", s.last_request_at ? fmtTime(s.last_request_at) : "-", BarChart3],
  ] as const;
});

function fmtTime(ts?: number | null) {
  return ts ? new Date(ts * 1000).toLocaleString("ru-RU") : "-";
}

function sum<T extends Record<string, unknown>>(rows: T[], key: keyof T) {
  return rows.reduce((acc, row) => acc + Number(row[key] || 0), 0);
}

function statusClass(status: string) {
  if (status === "online") return "text-emerald-400";
  if (status === "disabled") return "text-amber-300";
  return "text-red-400";
}

function queuePercent(row: QueueRow) {
  return Math.min(100, (Number(row.waiting || 0) / Math.max(1, Number(row.max_queue_size || 1))) * 100);
}

async function loadDashboard() {
  isLoading.value = true;
  loadError.value = "";
  try {
    const res = await fetch("/manager/api/dashboard", { credentials: "same-origin" });
    if (res.status === 401) {
      isLocked.value = true;
      pinMessage.value = "Введите PIN-код.";
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    dashboard.value = await res.json();
    refreshTime.value = new Date().toLocaleTimeString("ru-RU");
    isLocked.value = false;
    pinMessage.value = "";
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err);
  } finally {
    isLoading.value = false;
  }
}

async function login() {
  pinMessage.value = "Проверяю PIN...";
  try {
    const res = await fetch("/manager/api/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin: pin.value.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      pin.value = "";
      pinMessage.value = data.message || `HTTP ${res.status}`;
      return;
    }
    pin.value = "";
    isLocked.value = false;
    await loadDashboard();
  } catch (err) {
    pinMessage.value = err instanceof Error ? err.message : String(err);
  }
}

async function logout() {
  await fetch("/manager/api/logout", { method: "POST", credentials: "same-origin" });
  dashboard.value = null;
  isLocked.value = true;
  pinMessage.value = "Сессия закрыта.";
}

onMounted(() => {
  loadDashboard();
  timer = window.setInterval(loadDashboard, 10000);
});
onUnmounted(() => {
  if (timer) window.clearInterval(timer);
});
</script>

<template>
  <div class="min-h-screen overflow-x-hidden bg-background text-foreground">
    <div class="fixed inset-0 -z-10 bg-[radial-gradient(circle_at_78%_18%,rgba(109,93,106,.38),transparent_30rem),radial-gradient(circle_at_8%_74%,rgba(0,89,103,.32),transparent_34rem),linear-gradient(180deg,#080b10_0%,#05070a_100%)]" />
    <div class="fixed inset-0 -z-10 bg-[linear-gradient(90deg,rgba(0,0,0,.50),transparent_34%,rgba(0,0,0,.42))]" />

    <main v-if="isLocked" class="grid min-h-screen place-items-center px-4">
      <Card class="w-full max-w-[420px] border-white/10 bg-black/40 shadow-2xl backdrop-blur-xl">
        <CardHeader>
          <div class="mb-3 flex h-10 w-10 items-center justify-center rounded-md border border-white/15 bg-white/5 text-emerald-400">
            <LockKeyhole class="h-5 w-5" />
          </div>
          <CardTitle>LLM Gateway Manager</CardTitle>
        </CardHeader>
        <CardContent>
          <form class="space-y-3" @submit.prevent="login">
            <p class="text-sm text-muted-foreground">Введите PIN-код для доступа к панели загрузки, статистики и очередей.</p>
            <Input v-model="pin" type="password" inputmode="numeric" placeholder="PIN-код" class="bg-black/30" />
            <Button type="submit" :is-loading="pinMessage === 'Проверяю PIN...'" loading-text="Проверяю">Войти</Button>
            <p class="min-h-5 text-xs text-muted-foreground">{{ pinMessage }}</p>
          </form>
        </CardContent>
      </Card>
    </main>

    <template v-else>
      <header class="sticky top-0 z-30 grid h-12 grid-cols-[280px_minmax(0,1fr)_280px] items-center border-b border-white/10 bg-black/50 backdrop-blur-xl max-[1180px]:grid-cols-[220px_1fr_auto] max-[820px]:grid-cols-[1fr_auto]">
        <a href="#overview" class="flex h-12 items-center gap-2 px-5 text-xs font-bold">
          <span class="grid h-6 w-6 place-items-center border border-white/20 bg-white/5 text-[11px] text-emerald-400 [clip-path:polygon(50%_0,100%_26%,100%_74%,50%_100%,0_74%,0_26%)]">S</span>
          SIGMA-UI
        </a>
        <nav class="flex items-center gap-5 text-xs text-muted-foreground max-[820px]:hidden">
          <a href="#overview">Home</a>
          <a href="#load">Docs</a>
          <a href="#queues" class="font-semibold text-foreground">Components</a>
          <a href="#stats" class="inline-flex items-center gap-2">Blocks <Badge variant="secondary">Alpha</Badge></a>
          <a href="#errors" class="inline-flex items-center gap-2">Changelog <Badge variant="secondary">v2</Badge></a>
        </nav>
        <div class="flex justify-end gap-2 px-4">
          <Button variant="outline" size="sm" :is-loading="isLoading" @click="loadDashboard"><RefreshCcw class="mr-2 h-3.5 w-3.5" />Refresh</Button>
          <Button variant="outline" size="sm" @click="logout"><LogOut class="mr-2 h-3.5 w-3.5" />Logout</Button>
        </div>
      </header>

      <div class="mx-auto grid min-h-[calc(100vh-48px)] grid-cols-[280px_minmax(0,760px)_280px] justify-center max-[1180px]:grid-cols-[220px_minmax(0,1fr)] max-[820px]:block">
        <aside class="sticky top-12 h-[calc(100vh-48px)] overflow-auto border-r border-white/10 bg-black/15 px-4 py-4 text-xs text-muted-foreground max-[820px]:hidden">
          <div class="mb-4 h-[22px] border border-emerald-400/20 bg-gradient-to-r from-emerald-400/80 to-emerald-400/20 shadow-[0_0_22px_rgba(52,211,153,.18)]" />
          <p class="mb-2 font-semibold text-foreground">Config options</p>
          <a class="nav-link active" href="#overview">gateway <span>live</span></a>
          <a class="nav-link" href="#load">load <span>60m</span></a>
          <a class="nav-link" href="#queues">queue <span>1x</span></a>
          <p class="mb-2 mt-5 font-semibold text-foreground">Components</p>
          <a class="nav-link" href="#backends">Backend status</a>
          <a class="nav-link" href="#models">Published models</a>
          <a class="nav-link" href="#stats">Recent requests</a>
          <a class="nav-link" href="#errors">Recent errors</a>
          <p class="mb-2 mt-5 font-semibold text-foreground">Instructions</p>
          <div class="nav-link">PIN attempts <span>3</span></div>
          <div class="nav-link">Ban window <span>30m</span></div>
          <div class="nav-link">Per model <span>1 active</span></div>
        </aside>

        <section class="min-w-0 px-6 py-9 pb-20 max-[820px]:px-4">
          <header id="overview" class="mb-7 border-b border-white/10 pb-6">
            <p class="mb-2 text-xs text-muted-foreground">Gateway manager</p>
            <h1 class="text-[31px] font-bold leading-tight tracking-normal">LLM Gateway</h1>
            <p class="mt-2 max-w-2xl text-sm text-muted-foreground">Загрузка, историческая статистика, backend status и очередь запросов в темной теме Sigma UI.</p>
          </header>

          <div v-if="loadError" class="mb-4 rounded-md border border-red-400/20 bg-red-500/10 p-3 text-sm text-red-100">{{ loadError }}</div>

          <div class="mb-6 grid overflow-hidden rounded-lg border border-white/10 bg-white/10 sm:grid-cols-2 lg:grid-cols-5">
            <div v-for="[label, value, Icon] in summaryCards" :key="String(label)" class="border-white/10 bg-black/25 p-4 sm:border-r">
              <div class="flex items-center justify-between text-xs text-muted-foreground">{{ label }}<component :is="Icon" class="h-3.5 w-3.5" /></div>
              <div class="mt-2 truncate text-2xl font-semibold">{{ value }}</div>
            </div>
          </div>

          <div id="load" class="grid gap-4 lg:grid-cols-[1.1fr_1fr]">
            <Card class="border-white/10 bg-black/30 backdrop-blur-xl">
              <CardHeader class="border-b border-white/10 pb-3"><div class="flex items-center justify-between"><CardTitle class="text-lg">Текущая загрузка</CardTitle><Badge variant="secondary">{{ activeQueues }} active / {{ waitingRequests }} waiting</Badge></div></CardHeader>
              <CardContent class="grid gap-px overflow-hidden p-0 sm:grid-cols-3">
                <div class="bg-white/[.025] p-4"><p class="text-xs text-muted-foreground">Requests this minute</p><p class="mt-2 text-2xl font-semibold">{{ lastBucket?.total ?? 0 }}</p><p class="mt-1 text-xs text-muted-foreground">{{ lastBucket?.errors ?? 0 }} errors</p></div>
                <div class="bg-white/[.025] p-4"><p class="text-xs text-muted-foreground">Active backends</p><p class="mt-2 text-2xl font-semibold">{{ activeQueues }}</p><p class="mt-1 text-xs text-muted-foreground">{{ waitingRequests }} waiting</p></div>
                <div class="bg-white/[.025] p-4"><p class="text-xs text-muted-foreground">Queue rejects</p><p class="mt-2 text-2xl font-semibold">{{ sum(queues, "full_total") }}</p><p class="mt-1 text-xs text-muted-foreground">{{ sum(queues, "timeout_total") }} timeouts</p></div>
              </CardContent>
            </Card>
            <Card class="border-white/10 bg-black/30 backdrop-blur-xl">
              <CardHeader class="border-b border-white/10 pb-3"><div class="flex items-center justify-between"><CardTitle class="text-lg">Историческая загрузка</CardTitle><Badge variant="secondary">last 60m</Badge></div></CardHeader>
              <CardContent>
                <div class="flex h-[120px] items-end gap-[3px] border-t border-white/10 pt-3">
                  <span v-for="row in buckets.slice(-60)" :key="row.ts" class="min-w-[3px] flex-1 rounded-t-sm bg-gradient-to-b from-emerald-400 to-emerald-400/30" :class="{ 'from-red-400 to-red-500/40': row.errors }" :style="{ height: `${Math.max(4, Math.round(((row.total || 0) / maxBucketTotal) * 110))}px` }" :title="`${fmtTime(row.ts)} - ${row.total} req, ${row.errors} errors`" />
                  <span v-if="!buckets.length" class="text-sm text-muted-foreground">Истории пока нет.</span>
                </div>
              </CardContent>
            </Card>
          </div>

          <div class="mt-6 grid gap-4 lg:grid-cols-[1.1fr_1fr]">
            <Card id="backends" class="border-white/10 bg-black/30 backdrop-blur-xl">
              <CardHeader class="border-b border-white/10 pb-3"><div class="flex items-center justify-between"><CardTitle class="text-lg">Backend статус</CardTitle><Badge variant="secondary">refresh {{ refreshTime }}</Badge></div></CardHeader>
              <CardContent class="p-0"><Table><TableHeader><TableRow><TableHead>Backend</TableHead><TableHead>Тип</TableHead><TableHead>Статус</TableHead><TableHead>Latency</TableHead></TableRow></TableHeader><TableBody><TableRow v-for="row in dashboard?.backends ?? []" :key="row.name"><TableCell class="font-mono">{{ row.name }}</TableCell><TableCell>{{ row.kind }}</TableCell><TableCell :class="statusClass(row.status)">{{ row.status }}</TableCell><TableCell>{{ row.latency_ms == null ? "-" : `${row.latency_ms} ms` }}</TableCell></TableRow></TableBody></Table></CardContent>
            </Card>
            <Card id="models" class="border-white/10 bg-black/30 backdrop-blur-xl">
              <CardHeader class="border-b border-white/10 pb-3"><div class="flex items-center justify-between"><CardTitle class="text-lg">Публикуемые модели</CardTitle><Badge variant="secondary">{{ dashboard?.published_models.length ?? 0 }}</Badge></div></CardHeader>
              <CardContent class="p-0"><Table><TableHeader><TableRow><TableHead>Model</TableHead><TableHead>Source</TableHead><TableHead>Visible</TableHead></TableRow></TableHeader><TableBody><TableRow v-for="row in dashboard?.published_models ?? []" :key="row.id"><TableCell class="font-mono">{{ row.id }}</TableCell><TableCell>{{ row.owned_by || row.source || "-" }}</TableCell><TableCell><Badge v-if="row.visible" variant="secondary">UI</Badge><span v-else>-</span></TableCell></TableRow></TableBody></Table></CardContent>
            </Card>
          </div>

          <Card id="stats" class="mt-6 border-white/10 bg-black/30 backdrop-blur-xl">
            <CardHeader class="border-b border-white/10 pb-3"><div class="flex items-center justify-between"><CardTitle class="text-lg">Последние запросы</CardTitle><Badge variant="secondary">in-memory</Badge></div></CardHeader>
            <CardContent class="p-0"><Table><TableHeader><TableRow><TableHead>Время</TableHead><TableHead>Path</TableHead><TableHead>Model</TableHead><TableHead>Status</TableHead><TableHead>Queue</TableHead><TableHead>Duration</TableHead></TableRow></TableHeader><TableBody><TableRow v-for="row in dashboard?.stats.recent_requests ?? []" :key="`${row.ts}-${row.path}-${row.duration_ms}`"><TableCell>{{ fmtTime(row.ts) }}</TableCell><TableCell class="font-mono">{{ row.path }}</TableCell><TableCell class="font-mono">{{ row.model || "-" }}</TableCell><TableCell>{{ row.status }}</TableCell><TableCell>{{ row.queue_key ? `${row.queue_position ?? "-"} / ${row.queue_wait_ms ?? 0} ms` : "-" }}</TableCell><TableCell>{{ row.duration_ms == null ? "-" : `${row.duration_ms} ms` }}</TableCell></TableRow></TableBody></Table></CardContent>
          </Card>

          <Card id="queues" class="mt-6 border-white/10 bg-black/30 backdrop-blur-xl">
            <CardHeader class="border-b border-white/10 pb-3"><div class="flex items-center justify-between"><CardTitle class="text-lg">Очереди</CardTitle><Badge variant="secondary">1 active per backend</Badge></div></CardHeader>
            <CardContent class="p-0"><Table><TableHeader><TableRow><TableHead>Backend</TableHead><TableHead>Active</TableHead><TableHead>Waiting</TableHead><TableHead>Limit</TableHead><TableHead>Last wait</TableHead><TableHead>Totals</TableHead></TableRow></TableHeader><TableBody><TableRow v-for="row in queues" :key="row.queue_key"><TableCell class="font-mono">{{ row.queue_key }}</TableCell><TableCell><Badge v-if="row.active" variant="secondary">{{ row.active_model || "active" }}</Badge><span v-else>-</span></TableCell><TableCell>{{ row.waiting }}<div class="mt-2 h-2 overflow-hidden rounded-full border border-white/10 bg-white/10"><div class="h-full bg-emerald-400" :style="{ width: `${queuePercent(row)}%` }" /></div></TableCell><TableCell>{{ row.max_queue_size }} / {{ row.timeout_seconds }}s</TableCell><TableCell>{{ row.last_wait_ms || 0 }} ms</TableCell><TableCell class="text-muted-foreground">queued {{ row.queued_total }}, full {{ row.full_total }}, timeout {{ row.timeout_total }}</TableCell></TableRow></TableBody></Table></CardContent>
          </Card>

          <Card id="errors" class="mt-6 border-white/10 bg-black/30 backdrop-blur-xl">
            <CardHeader class="border-b border-white/10 pb-3"><div class="flex items-center justify-between"><CardTitle class="text-lg">Последние ошибки</CardTitle><Badge variant="secondary">max 20</Badge></div></CardHeader>
            <CardContent class="space-y-2">
              <div v-if="!(dashboard?.stats.recent_errors?.length)" class="text-sm text-muted-foreground">Ошибок пока нет.</div>
              <div v-for="err in dashboard?.stats.recent_errors ?? []" :key="`${err.ts}-${err.error}`" class="rounded-md border border-red-400/20 bg-red-500/10 p-3 text-sm text-red-100"><CircleAlert class="mr-2 inline h-4 w-4" /><strong>{{ fmtTime(err.ts) }}</strong> - {{ err.path }} - {{ err.error }}</div>
            </CardContent>
          </Card>
        </section>

        <aside class="sticky top-12 h-[calc(100vh-48px)] overflow-auto border-l border-white/10 bg-black/10 px-5 py-8 text-xs text-muted-foreground max-[1180px]:hidden">
          <p class="mb-2 font-semibold text-foreground">Table of content</p>
          <a class="toc-link" href="#overview">Overview</a><a class="toc-link" href="#load">Load</a><a class="toc-link" href="#backends">Backends</a><a class="toc-link" href="#models">Models</a><a class="toc-link" href="#stats">Statistics</a><a class="toc-link" href="#queues">Queue</a><a class="toc-link" href="#errors">Errors</a>
          <Separator class="my-5 bg-white/10" />
          <p class="mb-2 font-semibold text-foreground">Queue rules</p>
          <div class="toc-link"><GitBranch class="mr-2 h-3.5 w-3.5" />1 active request</div><div class="toc-link">others wait</div><div class="toc-link">position in headers</div>
        </aside>
      </div>
    </template>
  </div>
</template>
