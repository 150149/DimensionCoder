import {useEffect, useRef, useState} from 'react'
import {login as apiLogin, requestChallenge} from '../auth/api'
import {exportPublicKeyB64, generateKeyPair, listKeys, signChallenge, type StoredKey, storeKey,} from '../auth/keys'

interface Props {
  onLoggedIn: () => void
}

const PRIMARY_KEY_ID = 'primary'

export default function LoginPage({onLoggedIn}: Props) {
  const [key, setKey] = useState<StoredKey | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const autoTriedRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const keys = await listKeys()
        if (!cancelled) setKey(keys[0] ?? null)
        if (!cancelled && !autoTriedRef.current && keys[0]) {
          autoTriedRef.current = true
          void handleLogin(keys[0])
        }
      } catch {
        if (!cancelled) setError('读取本机密钥失败')
      }
      if (!cancelled) setLoaded(true)
    })()
    return () => {
      cancelled = true
    }
  }, [])

  async function handleGenerate() {
    setBusy(true)
    setError(null)
    try {
      const keyPair = await generateKeyPair()
      await storeKey(PRIMARY_KEY_ID, '本机密钥', keyPair)
      setKey({id: PRIMARY_KEY_ID, label: '本机密钥', createdAt: Date.now(), keyPair})
    } catch {
      setError('生成密钥失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleCopy() {
    if (!key) return
    setError(null)
    try {
      const b64 = await exportPublicKeyB64(key.keyPair)
      await navigator.clipboard.writeText(b64)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      setError('复制失败')
    }
  }

  async function handleLogin(k: StoredKey | null = key) {
    if (!k) return
    setBusy(true)
    setError(null)
    try {
      const {challenge} = await requestChallenge()
      const signature = await signChallenge(k.keyPair, challenge)
      await apiLogin(challenge, signature)
      onLoggedIn()
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : '登录失败')
    } finally {
      setBusy(false)
    }
  }

  if (!loaded) return <div style={{...s.page, color: 'var(--text-secondary, #6b7280)'}}>…</div>

  return (
      <div style={s.page}>
        <div style={s.card}>
          <h1 style={{margin: '0 0 20px', fontSize: 22, color: 'var(--text)'}}>
            登录 DimensionCoder
          </h1>

          {error && (
              <div style={{
                background: 'rgba(231,76,60,0.08)', border: '1px solid rgba(231,76,60,0.35)', color: '#d64533',
                borderRadius: 8, padding: '10px 12px', marginBottom: 16, fontSize: 13,
              }}>
                {error}
              </div>
          )}

          {!key ? (
              <button
                  className="btn btn-primary"
                  disabled={busy}
                  onClick={() => void handleGenerate()}
                  style={{width: '100%', padding: '12px 0', fontSize: 15}}
              >
                {busy ? '生成中…' : '生成密钥'}
              </button>
          ) : (
              <>
                <button
                    className="btn"
                    disabled={busy}
                    onClick={() => void handleCopy()}
                    style={{
                      width: '100%', padding: '10px 0', fontSize: 14,
                      background: copied ? 'var(--accent-soft, #eef4ff)' : 'var(--bg-subtle)',
                      color: copied ? 'var(--accent)' : undefined,
                    }}
                >
                  {copied ? '已复制' : '复制公钥配置'}
                </button>
                <button
                    className="btn btn-primary"
                    disabled={busy}
                    onClick={() => void handleLogin()}
                    style={{width: '100%', padding: '11px 0', fontSize: 15, marginTop: 10}}
                >
                  {busy ? '登录中…' : '登录'}
                </button>
                <div style={{textAlign: 'center', marginTop: 12, fontSize: 12}}>
                  <a
                      href="#"
                      onClick={(e) => {
                        e.preventDefault()
                        void handleGenerate()
                      }}
                      style={{color: 'var(--text-secondary, #6b7280)', textDecoration: 'none'}}
                  >
                    重新生成
                  </a>
                </div>
              </>
          )}
        </div>
      </div>
  )
}

const s: Record<string, React.CSSProperties> = {
  page: {
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    minHeight: '100vh', padding: 24, boxSizing: 'border-box',
    background: 'var(--bg)',
  },
  card: {
    maxWidth: 380, width: '100%', background: 'var(--bg-card)', border: '1px solid var(--border)',
    borderRadius: 12, padding: '32px 30px',
    boxShadow: '0 10px 40px rgba(0,0,0,0.06)',
  },
}
