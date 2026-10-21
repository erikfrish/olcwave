import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { Container } from '../../types'
import { containersApi } from '../../api/containers'
import { formatBytes, formatRate, formatUptime } from '../../utils/format'
import { useLanguage } from '../../i18n/useLanguage'
import StatusBadge from './StatusBadge'
import {
  PlayIcon,
  StopIcon,
  ArrowPathIcon,
  DocumentTextIcon,
  CodeBracketIcon,
  ChevronRightIcon,
  TrashIcon,
} from '@heroicons/react/24/outline'

type Action = 'run' | 'stop' | 'restart'

interface ContainerRowProps {
  container: Container
  expanded: boolean
  onToggle: () => void
  onLogs: (c: Container) => void
  onConfig: (c: Container) => void
  onRequestDelete: (c: Container) => void
  onError: (message: string) => void
  onSuccess: (message: string) => void
  /** Number of columns, so the expanded detail row can span the full table. */
  colSpan: number
}

export default function ContainerRow({
  container,
  expanded,
  onToggle,
  onLogs,
  onConfig,
  onRequestDelete,
  onError,
  onSuccess,
  colSpan,
}: ContainerRowProps) {
  const { t } = useLanguage()
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: ({ action }: { action: Action }) => {
      if (action === 'run') return containersApi.run(container.name)
      if (action === 'stop') return containersApi.stop(container.name)
      return containersApi.restart(container.name)
    },
    onSuccess: (_data, { action }) => {
      queryClient.invalidateQueries({ queryKey: ['containers-all'] })
      onSuccess(t('containerActionSuccess', { name: container.name, action }))
    },
    onError: (err: { response?: { data?: { detail?: string } } }, { action }) => {
      onError(err?.response?.data?.detail || t('failedToActionContainer', { name: container.name, action }))
    },
  })

  const pending = mutation.isPending ? mutation.variables?.action : undefined
  const isRunning = container.status === 'running'
  const created = new Date(container.created)

  const { data: stats } = useQuery({
    queryKey: ['container-stats', container.name],
    queryFn: () => containersApi.stats(container.name).then((r) => r.data),
    enabled: isRunning,
    refetchInterval: isRunning ? 5000 : false,
  })

  const stop = (e: React.MouseEvent) => e.stopPropagation()

  return (
    <>
      <tr
        onClick={onToggle}
        className="hover:bg-bg-hover transition-colors cursor-pointer align-middle"
        title={`${container.name}\n${container.id}`}
      >
        {/* 1. User ID (+ expand chevron) */}
        <Td>
          <div className="flex items-center gap-1.5">
            <ChevronRightIcon
              className={`w-3.5 h-3.5 shrink-0 text-text-muted transition-transform ${expanded ? 'rotate-90' : ''}`}
            />
            <code className="font-mono text-text-secondary">{container.short_uuid}</code>
          </div>
        </Td>
        {/* 2. Config Tag */}
        <Td>
          <code className="font-mono text-accent">{container.config_tag}</code>
        </Td>
        {/* 2b. Node (multi-node: source daemon of this container) */}
        {container.node && (
          <Td>
            <code className="font-mono text-xs text-text-muted">{container.node}</code>
          </Td>
        )}
        {/* 3. Created */}
        <Td className="text-text-secondary whitespace-nowrap">
          {created.toLocaleDateString()} {created.toLocaleTimeString()}
        </Td>
        {/* 4. Running For */}
        <Td className="tabular-nums whitespace-nowrap">{isRunning ? formatUptime(created) : '—'}</Td>
        {/* 5. Traffic Total */}
        <Td className="tabular-nums text-accent">{isRunning ? formatBytes(stats?.total_bytes ?? 0) : '—'}</Td>
        {/* 6. Traffic Download */}
        <Td className="tabular-nums">{isRunning ? formatBytes(stats?.download_bytes ?? 0) : '—'}</Td>
        {/* 7. Traffic Upload */}
        <Td className="tabular-nums">{isRunning ? formatBytes(stats?.upload_bytes ?? 0) : '—'}</Td>
        {/* 8. Download Speed */}
        <Td className="tabular-nums text-info">{isRunning ? formatRate(stats?.download_rate_bps ?? 0) : '—'}</Td>
        {/* 9. Upload Speed */}
        <Td className="tabular-nums text-success">{isRunning ? formatRate(stats?.upload_rate_bps ?? 0) : '—'}</Td>
        {/* Status */}
        <Td><StatusBadge status={container.status} /></Td>
        {/* Actions */}
        <td className="px-2.5 py-2.5 text-right" onClick={stop}>
          <div className="flex items-center justify-end gap-0.5">
            <ActionButton
              icon={isRunning ? StopIcon : PlayIcon}
              label={isRunning ? t('stop') : t('start')}
              onClick={() => mutation.mutate({ action: isRunning ? 'stop' : 'run' })}
              loading={pending === 'stop' || pending === 'run'}
              disabled={mutation.isPending}
              variant={isRunning ? 'danger' : 'success'}
            />
            <ActionButton
              icon={ArrowPathIcon}
              label={t('restart')}
              onClick={() => mutation.mutate({ action: 'restart' })}
              loading={pending === 'restart'}
              disabled={mutation.isPending}
            />
            <ActionButton icon={DocumentTextIcon} label={t('logs')} onClick={() => onLogs(container)} disabled={mutation.isPending} />
            <ActionButton icon={CodeBracketIcon} label={t('config')} onClick={() => onConfig(container)} disabled={mutation.isPending} />
            <ActionButton
              icon={TrashIcon}
              label={t('delete')}
              onClick={() => onRequestDelete(container)}
              disabled={mutation.isPending}
              variant="danger"
              iconOnly
            />
          </div>
        </td>
      </tr>

      {expanded && (
        <tr className="bg-bg-tertiary/30">
          <td colSpan={colSpan} className="px-5 py-4">
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-x-6 gap-y-3 text-xs animate-fade-in">
              <Detail label={t('containerName')} value={container.name} mono />
              <Detail label={t('containerId')} value={container.id} mono />
              <Detail label={t('image')} value={container.image} mono />
              <Detail label={t('status')} value={container.status} />
              <Detail label={t('userId')} value={container.short_uuid} mono />
              <Detail label={t('configTag')} value={container.config_tag} mono />
              <Detail label={t('created')} value={`${created.toLocaleDateString()} ${created.toLocaleTimeString()}`} />
              {isRunning && <Detail label={t('runningFor')} value={formatUptime(created)} />}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function Td({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <td className={`px-2.5 py-2.5 text-xs ${className}`}>{children}</td>
}

function Detail({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <p className="text-[10px] uppercase tracking-wider text-text-muted">{label}</p>
      <p className={`mt-0.5 text-text-secondary truncate ${mono ? 'font-mono' : ''}`} title={value}>
        {value}
      </p>
    </div>
  )
}

interface ActionButtonProps {
  icon: React.ComponentType<{ className?: string }>
  label: string
  onClick: () => void
  loading?: boolean
  disabled?: boolean
  variant?: 'default' | 'success' | 'danger'
  iconOnly?: boolean
}

const actionVariants = {
  default: 'text-text-secondary hover:text-text-primary hover:bg-bg-hover',
  success: 'text-success hover:bg-success/10',
  danger: 'text-danger hover:bg-danger/10',
}

function ActionButton({ icon: Icon, label, onClick, loading, disabled, variant = 'default', iconOnly }: ActionButtonProps) {
  return (
    <button
      onClick={onClick}
      disabled={disabled || loading}
      className={`inline-flex items-center gap-1.5 h-7 text-xs font-medium rounded-md
        transition-all duration-150 cursor-pointer active:scale-[0.98]
        disabled:opacity-40 disabled:pointer-events-none ${actionVariants[variant]}
        ${iconOnly ? 'w-7 px-0 justify-center' : 'px-1.5'}`}
      title={label}
    >
      {loading ? (
        <svg className="animate-spin h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
      ) : (
        <Icon className="w-3.5 h-3.5 shrink-0" />
      )}
      {!iconOnly && <span className="hidden xl:inline">{label}</span>}
    </button>
  )
}
