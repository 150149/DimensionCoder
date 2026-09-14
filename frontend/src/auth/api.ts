import {request} from '../api/client'

export interface AuthStatus {
  enabled: boolean
  authenticated: boolean
}

export function getAuthStatus(): Promise<AuthStatus> {
  return request<AuthStatus>('/api/auth/status')
}

export function requestChallenge(): Promise<{ challenge: string }> {
  return request<{ challenge: string }>('/api/auth/challenge', {method: 'POST'})
}

export function login(challenge: string, signature: string): Promise<{ status: string }> {
  return request<{ status: string }>('/api/auth/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({challenge, signature}),
  })
}

export function logout(): Promise<{ status: string }> {
  return request<{ status: string }>('/api/auth/logout', {method: 'POST'})
}
