import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  CalendarCheck2,
  ChevronLeft,
  ChevronRight,
  FileUp,
  History,
  RotateCcw,
} from "lucide-react"
import { useMemo, useState } from "react"

import {
  createCalendarGateway,
  parseCalendarImportFile,
} from "@/features/calendar/gateway"
import type {
  CalendarDay,
  CalendarGateway,
  CalendarImportFile,
  CalendarVersion,
} from "@/features/calendar/types"
import { ApiError } from "@/shared/api/client"
import { Badge } from "@/shared/ui/badge"
import { Button } from "@/shared/ui/button"
import { Checkbox } from "@/shared/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from "@/shared/ui/dialog"
import { Input } from "@/shared/ui/input"
import { PageState } from "@/shared/ui/page-state"

const defaultGateway = createCalendarGateway()
const weekdays = ["一", "二", "三", "四", "五", "六", "日"]
const statusLabels = {
  CONFIRMED: "已确认",
  PROVISIONAL: "待确认",
  OVERRIDDEN: "人工覆盖",
  MISSING: "缺失",
} as const

function dateText(date: Date) {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-")
}

function monthRange(month: Date) {
  const first = new Date(month.getFullYear(), month.getMonth(), 1)
  const last = new Date(month.getFullYear(), month.getMonth() + 1, 0)
  return { from: dateText(first), through: dateText(last) }
}

function shiftMonth(month: Date, offset: number) {
  return new Date(month.getFullYear(), month.getMonth() + offset, 1)
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Shanghai",
  }).format(new Date(value))
}

function currentVersion(versions: CalendarVersion[]) {
  return versions.find((item) => item.isCurrent)
    ?? versions.reduce<CalendarVersion | undefined>(
      (latest, item) => !latest || item.versionNumber > latest.versionNumber
        ? item
        : latest,
      undefined,
    )
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作失败，请稍后重试。"
}

export function CalendarPage({
  gateway = defaultGateway,
}: {
  gateway?: CalendarGateway
}) {
  const queryClient = useQueryClient()
  const [month, setMonth] = useState(() => new Date())
  const [selectedDay, setSelectedDay] = useState<CalendarDay | null>(null)
  const [restoreVersion, setRestoreVersion] = useState<CalendarVersion | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [reason, setReason] = useState("")
  const [note, setNote] = useState("")
  const [isTradingDay, setIsTradingDay] = useState(true)
  const [confirmed, setConfirmed] = useState(false)
  const [importFile, setImportFile] = useState<CalendarImportFile | null>(null)
  const [fileName, setFileName] = useState("")
  const [fileError, setFileError] = useState("")
  const range = useMemo(() => monthRange(month), [month])

  const snapshotQuery = useQuery({
    queryKey: ["trading-calendar", range.from, range.through],
    queryFn: () => gateway.loadSnapshot(range.from, range.through),
  })
  const snapshot = snapshotQuery.data
  const latestVersion = snapshot ? currentVersion(snapshot.versions) : undefined

  const finishWrite = async () => {
    await queryClient.invalidateQueries({ queryKey: ["trading-calendar"] })
    setSelectedDay(null)
    setRestoreVersion(null)
    setImportOpen(false)
    setReason("")
    setNote("")
    setConfirmed(false)
    setImportFile(null)
    setFileName("")
    setFileError("")
  }

  const overrideMutation = useMutation({
    mutationFn: () => {
      if (!selectedDay || !latestVersion) {
        throw new Error("当前日历版本不可用。")
      }
      return gateway.overrideDay({
        day: selectedDay,
        isTradingDay,
        expectedCurrentVersion: latestVersion.versionNumber,
        reason: reason.trim(),
        note: note.trim(),
      })
    },
    onSuccess: finishWrite,
  })
  const importMutation = useMutation({
    mutationFn: () => {
      if (!importFile) {
        throw new Error("请选择有效的日历文件。")
      }
      return gateway.importCalendar({
        file: importFile,
        expectedCurrentVersion: latestVersion?.versionNumber ?? null,
        reason: reason.trim(),
      })
    },
    onSuccess: finishWrite,
  })
  const restoreMutation = useMutation({
    mutationFn: () => {
      if (!restoreVersion || !latestVersion) {
        throw new Error("当前日历版本不可用。")
      }
      return gateway.restoreVersion({
        version: restoreVersion,
        expectedCurrentVersion: latestVersion.versionNumber,
        reason: reason.trim(),
      })
    },
    onSuccess: finishWrite,
  })

  const openOverride = (day: CalendarDay) => {
    setSelectedDay(day)
    setIsTradingDay(day.isTradingDay)
    setNote(day.note ?? "")
    setReason("")
    setConfirmed(false)
    overrideMutation.reset()
  }

  const openRestore = (version: CalendarVersion) => {
    setRestoreVersion(version)
    setReason("")
    setConfirmed(false)
    restoreMutation.reset()
  }

  const readImportFile = async (file: File | undefined) => {
    setImportFile(null)
    setFileError("")
    setFileName(file?.name ?? "")
    if (!file) return
    try {
      const value = JSON.parse(await file.text()) as unknown
      setImportFile(parseCalendarImportFile(value))
    } catch (error) {
      setFileError(
        error instanceof ApiError
          ? error.message
          : "文件必须是有效的 UTF-8 JSON。",
      )
    }
  }

  if (snapshotQuery.isPending) {
    return (
      <PageState
        state="loading"
        title="正在读取交易日历"
        description="正在核对正式日历、覆盖范围和版本历史。"
      />
    )
  }
  if (snapshotQuery.isError || !snapshot) {
    const code = snapshotQuery.error instanceof ApiError
      ? snapshotQuery.error.code
      : "CALENDAR_UNAVAILABLE"
    return (
      <PageState
        state="error"
        title="交易日历暂时无法读取"
        description={`错误代码：${code}`}
        action={{
          label: "重新加载",
          onClick: () => void snapshotQuery.refetch(),
        }}
      />
    )
  }

  const daysByDate = new Map(snapshot.days.map((day) => [day.tradeDate, day]))
  const firstDate = new Date(month.getFullYear(), month.getMonth(), 1)
  const dayCount = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate()
  const leadingBlanks = (firstDate.getDay() + 6) % 7
  const cells = Array.from(
    { length: leadingBlanks + dayCount },
    (_, index) => index < leadingBlanks ? null : index - leadingBlanks + 1,
  )
  const canImport = snapshot.allowedActions.includes("IMPORT")
  const today = dateText(new Date())

  return (
    <main className="workspace-page">
      <header className="workspace-page__header">
        <div>
          <p className="workspace-page__eyebrow">运行基准</p>
          <h1>交易日历</h1>
          <p>查看正式交易日、特殊时段和不可变版本，所有自动任务以这里为准。</p>
        </div>
        <Button
          onClick={() => {
            setImportOpen(true)
            setReason("")
            setConfirmed(false)
            setFileError("")
            importMutation.reset()
          }}
          disabled={!canImport}
        >
          <FileUp aria-hidden="true" />
          导入日历
        </Button>
      </header>

      <section className="grid gap-px border-y bg-border sm:grid-cols-2 xl:grid-cols-4">
        <CoverageMetric
          label="正式覆盖至"
          value={snapshot.coverage.confirmedThrough ?? "暂无"}
          tone={snapshot.coverage.missingToday ? "danger" : "normal"}
        />
        <CoverageMetric
          label="未来确认天数"
          value={`${snapshot.coverage.futureConfirmedDays} 天`}
          tone={snapshot.coverage.futureConfirmedDays < 30
            ? "danger"
            : snapshot.coverage.futureConfirmedDays < 60 ? "warning" : "normal"}
        />
        <CoverageMetric
          label="覆盖状态"
          value={snapshot.coverage.missingToday ? "今日缺失" : snapshot.coverage.level}
          tone={snapshot.coverage.missingToday ? "danger" : "normal"}
        />
        <CoverageMetric
          label="当前版本"
          value={latestVersion ? `v${latestVersion.versionNumber}` : "暂无"}
          tone="normal"
        />
      </section>

      <section className="mt-7 grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="min-w-0">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold">
                {month.getFullYear()} 年 {month.getMonth() + 1} 月
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                点击日期可查看来源、时段和覆盖影响。
              </p>
            </div>
            <div className="flex items-center gap-1">
              <Button
                variant="outline"
                size="icon"
                aria-label="上个月"
                onClick={() => setMonth((value) => shiftMonth(value, -1))}
              >
                <ChevronLeft aria-hidden="true" />
              </Button>
              <Button variant="outline" onClick={() => setMonth(new Date())}>本月</Button>
              <Button
                variant="outline"
                size="icon"
                aria-label="下个月"
                onClick={() => setMonth((value) => shiftMonth(value, 1))}
              >
                <ChevronRight aria-hidden="true" />
              </Button>
            </div>
          </div>

          <div className="grid grid-cols-7 border-l border-t" aria-label="月度交易日历">
            {weekdays.map((weekday) => (
              <div
                className="border-b border-r bg-muted/40 px-2 py-2 text-center text-xs font-semibold text-muted-foreground"
                key={weekday}
              >
                周{weekday}
              </div>
            ))}
            {cells.map((dayNumber, index) => {
              if (dayNumber === null) {
                return <div className="min-h-24 border-b border-r bg-muted/15" key={`blank-${index}`} />
              }
              const tradeDate = dateText(
                new Date(month.getFullYear(), month.getMonth(), dayNumber),
              )
              const day = daysByDate.get(tradeDate)
              return (
                <Button
                  variant="ghost"
                  className="group h-auto min-h-24 flex-col items-stretch justify-start rounded-none border-b border-r bg-background p-2 text-left hover:bg-muted/35 disabled:bg-muted/10"
                  type="button"
                  key={tradeDate}
                  disabled={!day}
                  onClick={() => day && openOverride(day)}
                  aria-label={`${tradeDate} ${day ? day.isTradingDay ? "交易日" : "休市" : "未录入"}`}
                >
                  <span className="flex items-start justify-between gap-1">
                    <strong className={tradeDate === today ? "text-primary" : ""}>
                      {dayNumber}
                    </strong>
                    {day ? (
                      <Badge variant={day.status === "MISSING" ? "destructive" : day.status === "CONFIRMED" ? "default" : "secondary"} className="h-1.5 min-w-1.5 rounded-full p-0" aria-hidden="true" />
                    ) : null}
                  </span>
                  <span className="mt-4 block text-xs font-medium">
                    {day ? day.isTradingDay ? "交易" : "休市" : "未录入"}
                  </span>
                  <span className="mt-1 block truncate text-[11px] text-muted-foreground">
                    {day?.sessions.length
                      ? day.sessions.map((item) => item.startsAt.slice(0, 5)).join(" / ")
                      : day ? statusLabels[day.status] : "阻止自动调度"}
                  </span>
                </Button>
              )
            })}
          </div>
        </div>

        <aside className="border-l pl-0 xl:pl-6">
          <div className="flex items-center gap-2">
            <History className="size-4" aria-hidden="true" />
            <h2 className="text-lg font-semibold">版本历史</h2>
          </div>
          <div className="mt-4 grid gap-0 border-t">
            {snapshot.versions.length === 0 ? (
              <p className="border-b py-8 text-sm text-muted-foreground">暂无日历版本</p>
            ) : snapshot.versions.map((version) => (
              <article className="border-b py-4" key={version.id}>
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <strong>v{version.versionNumber}</strong>
                      {version.isCurrent ? (
                        <Badge variant="secondary">当前</Badge>
                      ) : null}
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {version.source} · {version.sourceVersion}
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`恢复版本 v${version.versionNumber}`}
                    title="恢复此版本"
                    disabled={!version.allowedActions.includes("RESTORE")}
                    onClick={() => openRestore(version)}
                  >
                    <RotateCcw aria-hidden="true" />
                  </Button>
                </div>
                <p className="mt-3 text-sm">{version.reason || "未填写版本说明"}</p>
                <p className="mt-2 text-xs text-muted-foreground">
                  {formatDateTime(version.createdAt)}
                </p>
              </article>
            ))}
          </div>
        </aside>
      </section>

      <OverrideDialog
        day={selectedDay}
        isTradingDay={isTradingDay}
        setIsTradingDay={setIsTradingDay}
        reason={reason}
        setReason={setReason}
        note={note}
        setNote={setNote}
        confirmed={confirmed}
        setConfirmed={setConfirmed}
        pending={overrideMutation.isPending}
        error={overrideMutation.isError ? errorMessage(overrideMutation.error) : ""}
        onClose={() => !overrideMutation.isPending && setSelectedDay(null)}
        onSubmit={() => overrideMutation.mutate()}
      />

      <Dialog open={importOpen} onOpenChange={(open) => !importMutation.isPending && setImportOpen(open)}>
        <DialogContent>
          <DialogTitle>导入正式交易日历</DialogTitle>
          <DialogDescription>
            文件会被完整校验；任意日期、状态或时段错误都会拒绝整个版本。
          </DialogDescription>
          <label className="grid gap-2 text-sm font-medium">
            UTF-8 JSON 文件
            <Input
              type="file"
              accept="application/json,.json"
              onChange={(event) => void readImportFile(event.target.files?.[0])}
            />
          </label>
          {fileName ? (
            <p className="text-xs text-muted-foreground">
              {fileName}{importFile ? ` · ${importFile.days.length} 个日期` : ""}
            </p>
          ) : null}
          {fileError ? <p role="alert" className="text-sm text-destructive">{fileError}</p> : null}
          <ReasonAndConfirm
            reason={reason}
            setReason={setReason}
            confirmed={confirmed}
            setConfirmed={setConfirmed}
            confirmation="我确认导入会创建新版本并切换正式日历。"
          />
          {importMutation.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {errorMessage(importMutation.error)}
            </p>
          ) : null}
          <DialogFooter>
            <Button variant="outline" disabled={importMutation.isPending} onClick={() => setImportOpen(false)}>
              取消
            </Button>
            <Button
              disabled={!importFile || !reason.trim() || !confirmed || importMutation.isPending}
              onClick={() => importMutation.mutate()}
            >
              {importMutation.isPending ? "正在导入" : "确认导入"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={restoreVersion !== null}
        onOpenChange={(open) => !open && !restoreMutation.isPending && setRestoreVersion(null)}
      >
        <DialogContent>
          <DialogTitle>恢复日历版本 v{restoreVersion?.versionNumber}</DialogTitle>
          <DialogDescription>
            恢复不会删除历史，而是基于所选版本创建一个新的正式版本。
          </DialogDescription>
          <ReasonAndConfirm
            reason={reason}
            setReason={setReason}
            confirmed={confirmed}
            setConfirmed={setConfirmed}
            confirmation="我确认恢复可能改变未来任务安排，历史任务不会被删除。"
          />
          {restoreMutation.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {errorMessage(restoreMutation.error)}
            </p>
          ) : null}
          <DialogFooter>
            <Button variant="outline" disabled={restoreMutation.isPending} onClick={() => setRestoreVersion(null)}>
              取消
            </Button>
            <Button
              disabled={!reason.trim() || !confirmed || restoreMutation.isPending}
              onClick={() => restoreMutation.mutate()}
            >
              {restoreMutation.isPending ? "正在恢复" : "确认恢复"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  )
}

function CoverageMetric({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone: "normal" | "warning" | "danger"
}) {
  return (
    <article className="bg-background px-5 py-4">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <div className="mt-2 flex items-center justify-between gap-3">
        <strong className="text-lg">{value}</strong>
        <Badge variant={tone === "danger" ? "destructive" : tone === "warning" ? "secondary" : "default"}>
          {tone === "danger" ? "异常" : tone === "warning" ? "关注" : "正常"}
        </Badge>
      </div>
    </article>
  )
}

function ReasonAndConfirm({
  reason,
  setReason,
  confirmed,
  setConfirmed,
  confirmation,
}: {
  reason: string
  setReason: (value: string) => void
  confirmed: boolean
  setConfirmed: (value: boolean) => void
  confirmation: string
}) {
  return (
    <>
      <label className="grid gap-2 text-sm font-medium">
        操作原因
        <Input
          maxLength={500}
          value={reason}
          placeholder="请说明本次日历变更原因"
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      <label className="flex items-start gap-2 text-sm">
        <Checkbox
          className="mt-1"
          checked={confirmed}
          onCheckedChange={(checked) => setConfirmed(checked === true)}
        />
        <span>{confirmation}</span>
      </label>
    </>
  )
}

function OverrideDialog({
  day,
  isTradingDay,
  setIsTradingDay,
  reason,
  setReason,
  note,
  setNote,
  confirmed,
  setConfirmed,
  pending,
  error,
  onClose,
  onSubmit,
}: {
  day: CalendarDay | null
  isTradingDay: boolean
  setIsTradingDay: (value: boolean) => void
  reason: string
  setReason: (value: string) => void
  note: string
  setNote: (value: string) => void
  confirmed: boolean
  setConfirmed: (value: boolean) => void
  pending: boolean
  error: string
  onClose: () => void
  onSubmit: () => void
}) {
  const isPast = day ? day.tradeDate < dateText(new Date()) : false
  return (
    <Dialog open={day !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogTitle>{day?.tradeDate} 日历覆盖</DialogTitle>
        <DialogDescription>
          当前为{day?.isTradingDay ? "交易日" : "休市日"}，状态为
          {day ? statusLabels[day.status] : "未知"}，来源 {day?.source ?? "未知"}。
        </DialogDescription>
        <div className="grid gap-3 sm:grid-cols-2" role="group" aria-label="日期类型">
          <Button
            type="button"
            variant={isTradingDay ? "default" : "outline"}
            onClick={() => setIsTradingDay(true)}
          >
            <CalendarCheck2 aria-hidden="true" />
            设为交易日
          </Button>
          <Button
            type="button"
            variant={!isTradingDay ? "default" : "outline"}
            onClick={() => setIsTradingDay(false)}
          >
            设为休市日
          </Button>
        </div>
        <p className="border-l-2 border-amber-500 pl-3 text-sm text-muted-foreground">
          {isPast
            ? "该日期已经过去：系统不会补跑错过的任务，只会保留历史并记录修正。"
            : isTradingDay
              ? "未来时间将按交易日正常调度；已经过去的时点不会补跑。"
              : "未分发或排队中的未来任务会取消，运行中的行情仅保留诊断结果。"}
        </p>
        <label className="grid gap-2 text-sm font-medium">
          说明
          <Input
            maxLength={500}
            value={note}
            placeholder="可记录节假日、临时休市或特殊安排"
            onChange={(event) => setNote(event.target.value)}
          />
        </label>
        <ReasonAndConfirm
          reason={reason}
          setReason={setReason}
          confirmed={confirmed}
          setConfirmed={setConfirmed}
          confirmation="我确认本次覆盖会创建新版本，并可能影响未来任务。"
        />
        {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="outline" disabled={pending} onClick={onClose}>取消</Button>
          <Button
            disabled={!reason.trim() || !confirmed || pending || !day?.allowedActions.includes("OVERRIDE")}
            onClick={onSubmit}
          >
            {pending ? "正在保存" : "确认覆盖"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
