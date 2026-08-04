import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Clock3, Plus, Trash2 } from "lucide-react"
import { useState } from "react"

import type { MonitorSchedule, MonitoringGateway } from "@/features/monitoring/types"
import { Alert, AlertDescription } from "@/shared/ui/alert"
import { Button } from "@/shared/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/shared/ui/card"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/shared/ui/field"
import { Input } from "@/shared/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/shared/ui/select"
import { Spinner } from "@/shared/ui/spinner"

const NEW_SCHEDULE = "__new__"

function isTradingTime(value: string): boolean {
  return (value >= "09:30" && value <= "11:30")
    || (value >= "13:00" && value <= "15:00")
}

export function MonitorSchedulePanel({ gateway }: { gateway: MonitoringGateway }) {
  const schedulesQuery = useQuery({
    queryKey: ["monitoring", "schedules"],
    queryFn: () => gateway.loadSchedules(),
  })
  const [selectedId, setSelectedId] = useState("")
  const effectiveId = selectedId || schedulesQuery.data?.[0]?.id || NEW_SCHEDULE
  const selected = schedulesQuery.data?.find((schedule) => schedule.id === effectiveId)

  const chooseSchedule = (value: string) => {
    setSelectedId(value)
  }

  return (
    <Card aria-label="盘中监控时间配置">
      <CardHeader className="flex-row items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <CardTitle className="flex items-center gap-2"><Clock3 aria-hidden="true" />盘中监控时间</CardTitle>
          <CardDescription>只在确认交易日的连续交易时段触发。这里配置具体时间，不使用 Cron 表达式；停机错过的时间不会补跑。</CardDescription>
        </div>
        <Button type="button" variant="outline" onClick={() => chooseSchedule(NEW_SCHEDULE)}><Plus data-icon="inline-start" />新建计划</Button>
      </CardHeader>
      <CardContent>
        {schedulesQuery.isPending ? <div className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner />正在读取监控时间</div> : null}
        {schedulesQuery.isError ? <Alert variant="destructive"><AlertDescription>监控时间读取失败，请稍后重试。</AlertDescription></Alert> : null}
        {!schedulesQuery.isPending && !schedulesQuery.isError ? <FieldGroup>
          {schedulesQuery.data?.length ? <Field><FieldLabel htmlFor="monitor-schedule-select">监控计划</FieldLabel><Select value={effectiveId} onValueChange={chooseSchedule}><SelectTrigger id="monitor-schedule-select"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{schedulesQuery.data.map((schedule) => <SelectItem key={schedule.id} value={schedule.id}>{schedule.name}</SelectItem>)}</SelectGroup></SelectContent></Select></Field> : null}
          <ScheduleEditor key={effectiveId} gateway={gateway} schedule={selected} />
        </FieldGroup> : null}
      </CardContent>
    </Card>
  )
}

function ScheduleEditor({ gateway, schedule }: { gateway: MonitoringGateway; schedule?: MonitorSchedule }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState(schedule?.name ?? "默认盘中监控")
  const [times, setTimes] = useState<string[]>(schedule?.times ?? [])
  const [reason, setReason] = useState("")
  const saveMutation = useMutation({
    mutationFn: () => gateway.saveSchedule({
      id: schedule?.id,
      name: name.trim(),
      version: schedule?.version,
      times: [...new Set(times)].sort(),
      reason: reason.trim(),
    }),
    onSuccess: async () => {
      setReason("")
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["monitoring", "schedules"] }),
        queryClient.invalidateQueries({ queryKey: ["monitoring", "overview"] }),
      ])
    },
  })
  const timesValid = times.length > 0
    && times.length <= 20
    && times.every(isTradingTime)
    && new Set(times).size === times.length

  return <>
    <Field><FieldLabel htmlFor="monitor-schedule-name">计划名称</FieldLabel><Input id="monitor-schedule-name" maxLength={100} value={name} onChange={(event) => setName(event.target.value)} /></Field>
    <Field>
      <FieldLabel>检查时间</FieldLabel>
      <FieldDescription>最多 20 个时间，只允许 09:30～11:30、13:00～15:00。</FieldDescription>
      <div className="flex flex-col gap-2">{times.map((value, index) => <div className="flex items-center gap-2" key={index}><Input aria-label={`检查时间 ${index + 1}`} type="time" value={value} onChange={(event) => setTimes((current) => current.map((item, itemIndex) => itemIndex === index ? event.target.value : item))} /><Button type="button" size="icon-sm" variant="ghost" aria-label={`删除检查时间 ${index + 1}`} onClick={() => setTimes((current) => current.filter((_, itemIndex) => itemIndex !== index))}><Trash2 aria-hidden="true" /></Button></div>)}</div>
      <Button type="button" variant="outline" disabled={times.length >= 20} onClick={() => setTimes((current) => [...current, "09:30"])}><Plus data-icon="inline-start" />添加时间</Button>
    </Field>
    <Field><FieldLabel htmlFor="monitor-schedule-reason">修改原因</FieldLabel><Input id="monitor-schedule-reason" maxLength={500} placeholder="例如：调整收盘前检查时间" value={reason} onChange={(event) => setReason(event.target.value)} /></Field>
    {!timesValid && times.length ? <Alert variant="destructive"><AlertDescription>检查时间必须位于交易时段内，并且不能重复。</AlertDescription></Alert> : null}
    {saveMutation.isError ? <Alert variant="destructive"><AlertDescription>保存失败，请检查时间、权限或计划版本后重试。</AlertDescription></Alert> : null}
    <Button type="button" disabled={!name.trim() || !reason.trim() || !timesValid || saveMutation.isPending} onClick={() => saveMutation.mutate()}>{saveMutation.isPending ? <><Spinner data-icon="inline-start" />保存中</> : "保存监控时间"}</Button>
  </>
}
