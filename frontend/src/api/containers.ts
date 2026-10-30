import api from './client'
import type { ContainerLogs, ContainerConfig, ContainerStats, ContainersList } from '../types'

export const containersApi = {
  getAllStatus: () =>
    api.get<ContainersList>('/containers/all_status'),

  run: (name: string) =>
    api.post<string>('/containers/run', null, { params: { name } }),

  stop: (name: string) =>
    api.post<string>('/containers/stop', null, { params: { name } }),

  restart: (name: string) =>
    api.post<string>('/containers/restart', null, { params: { name } }),

  remove: (name: string) =>
    api.delete('/containers/', { params: { name } }),

  logs: (name: string) =>
    api.get<ContainerLogs>('/containers/logs', { params: { name } }),

  getConfig: (name: string) =>
    api.get<ContainerConfig>('/containers/config', { params: { name } }),

  stats: (name: string) =>
    api.get<ContainerStats>('/containers/stats', { params: { name } }),
}
