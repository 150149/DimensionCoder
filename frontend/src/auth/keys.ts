export interface StoredKey {
    id: string
    label: string
    createdAt: number
    keyPair: CryptoKeyPair
}

const DB_NAME = 'dc-auth-keys'
const STORE = 'keys'

function openDb(): Promise<IDBDatabase> {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, 1)
        req.onupgradeneeded = () => {
            if (!req.result.objectStoreNames.contains(STORE)) {
                req.result.createObjectStore(STORE, {keyPath: 'id'})
            }
        }
        req.onsuccess = () => resolve(req.result)
        req.onerror = () => reject(req.error)
    })
}

export async function generateKeyPair(): Promise<CryptoKeyPair> {
    return crypto.subtle.generateKey(
        {name: 'ECDSA', namedCurve: 'P-521'},
        false,
        ['sign', 'verify'],
    )
}

export function newKeyId(): string {
    return crypto.randomUUID ? crypto.randomUUID() : `key-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

export async function storeKey(id: string, label: string, keyPair: CryptoKeyPair): Promise<void> {
    const db = await openDb()
    return new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readwrite')
        tx.objectStore(STORE).put({id, label, createdAt: Date.now(), keyPair} as StoredKey)
        tx.oncomplete = () => resolve()
        tx.onerror = () => reject(tx.error)
    })
}

export async function listKeys(): Promise<StoredKey[]> {
    const db = await openDb()
    return new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readonly')
        const req = tx.objectStore(STORE).getAll()
        req.onsuccess = () => {
            const rows = (req.result ?? []) as StoredKey[]
            resolve(rows.sort((a, b) => b.createdAt - a.createdAt))
        }
        req.onerror = () => reject(req.error)
    })
}

export async function getKey(id: string): Promise<StoredKey | null> {
    const db = await openDb()
    return new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readonly')
        const req = tx.objectStore(STORE).get(id)
        req.onsuccess = () => resolve((req.result as StoredKey) ?? null)
        req.onerror = () => reject(req.error)
    })
}

export async function deleteKey(id: string): Promise<void> {
    const db = await openDb()
    return new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readwrite')
        tx.objectStore(STORE).delete(id)
        tx.oncomplete = () => resolve()
        tx.onerror = () => reject(tx.error)
    })
}

export async function exportPublicKeyB64(keyPair: CryptoKeyPair): Promise<string> {
    const spki = await crypto.subtle.exportKey('spki', keyPair.publicKey)
    return btoa(String.fromCharCode(...new Uint8Array(spki)))
}

export async function exportPublicKeyPem(keyPair: CryptoKeyPair): Promise<string> {
    const b64 = await exportPublicKeyB64(keyPair)
    const lines = b64.match(/.{1,64}/g) ?? []
    return `-----BEGIN PUBLIC KEY-----\n${lines.join('\n')}\n-----END PUBLIC KEY-----`
}

export async function signChallenge(keyPair: CryptoKeyPair, challenge: string): Promise<string> {
    const sig = await crypto.subtle.sign(
        {name: 'ECDSA', hash: 'SHA-512'},
        keyPair.privateKey,
        new TextEncoder().encode(challenge),
    )
    return btoa(String.fromCharCode(...new Uint8Array(sig)))
}

export async function verifyWithPublicKey(publicKey: CryptoKey, challenge: string, signatureB64: string): Promise<boolean> {
    try {
        const sig = Uint8Array.from(atob(signatureB64), (c) => c.charCodeAt(0))
        return await crypto.subtle.verify(
            {name: 'ECDSA', hash: 'SHA-512'},
            publicKey,
            sig,
            new TextEncoder().encode(challenge),
        )
    } catch {
        return false
    }
}
