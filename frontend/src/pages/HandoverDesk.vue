<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'
import { FIXTURE_KEYS } from '../fixture_keys'
import { hmacSha256Hex } from '../sign'

const shifts = ref([])
const fromId = ref(1)
const toId = ref(2)
const current = ref(null)
const msg = ref('')
const ok = ref(false)

function attested(party) {
  return (current.value?.attestations || []).some((a) => a.party === party)
}

async function refresh() {
  shifts.value = await getJson('/api/shifts')
  const logs = await getJson('/api/handovers')
  current.value = logs.find((h) => h.status === '进行中') || null
  if (current.value) {
    current.value = await getJson(`/api/handovers/${current.value.id}`)
  }
}

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
})

async function start() {
  msg.value = ''
  ok.value = false
  try {
    current.value = await postJson('/api/handovers/start', {
      from_shift_id: Number(fromId.value),
      to_shift_id: Number(toId.value),
    })
    ok.value = true
    msg.value = `已开交班单 #${current.value.id}，清单 ${current.value.batch_count} 批，摘要已固化，待双方签注`
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}

async function attest(shiftId) {
  if (!current.value) return
  msg.value = ''
  ok.value = false
  try {
    const secret = FIXTURE_KEYS[shiftId]
    if (!secret) throw new Error(`班次 ${shiftId} 无本地夹具密钥`)
    const signature = await hmacSha256Hex(secret, current.value.attest_payload)
    current.value = await postJson(`/api/handovers/${current.value.id}/attest`, {
      shift_id: shiftId,
      signature,
    })
    ok.value = true
    msg.value = `班次 ${shiftId} 签注已记录（摘要 ${current.value.manifest_digest.slice(0, 12)}…）`
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}

async function complete() {
  if (!current.value) return
  msg.value = ''
  ok.value = false
  try {
    current.value = await postJson(`/api/handovers/${current.value.id}/complete`)
    ok.value = current.value.status === '已完成'
    msg.value = current.value.status === '已完成'
      ? `交班单 #${current.value.id} 已完成，双方签注封存，批次已过户`
      : `交班单 #${current.value.id} 失败：${current.value.reason || '归属不一致'}`
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}
</script>

<template>
  <div>
    <h1>交班办理</h1>
    <p class="lead">
      同一时刻只允许一单进行中的交班；过户前必须收齐交班方与接班方签注，
      签注绑定发起时清单摘要，清单被改动则拒绝过户。
    </p>

    <div class="panel">
      <div class="row">
        <label>交班方
          <select v-model.number="fromId">
            <option v-for="s in shifts" :key="s.id" :value="s.id">{{ s.name }} · {{ s.keeper }}</option>
          </select>
        </label>
        <label>接班方
          <select v-model.number="toId">
            <option v-for="s in shifts" :key="'t'+s.id" :value="s.id">{{ s.name }} · {{ s.keeper }}</option>
          </select>
        </label>
        <button class="btn" :disabled="!!current" @click="start">发起交班</button>
        <button
          class="btn"
          :disabled="!current || attested('from')"
          @click="attest(current.from_shift_id)"
        >交班方签注</button>
        <button
          class="btn"
          :disabled="!current || attested('to')"
          @click="attest(current.to_shift_id)"
        >接班方签注</button>
        <button
          class="btn ghost"
          :disabled="!current || !attested('from') || !attested('to')"
          @click="complete"
        >完成交班</button>
      </div>
      <p class="msg" :class="{ ok }">{{ msg }}</p>
    </div>

    <div class="panel" v-if="current">
      <h2>当前交班单 #{{ current.id }} · {{ current.status }}</h2>
      <p class="lead" style="margin-bottom:12px">
        {{ current.from_shift_name }} → {{ current.to_shift_name }} · 清单 {{ current.batch_count }} 批
        · 清单摘要 {{ (current.manifest_digest || '').slice(0, 16) }}…
        · 签注：交班方{{ attested('from') ? '已签' : '未签' }} / 接班方{{ attested('to') ? '已签' : '未签' }}
      </p>
      <table>
        <thead><tr><th>业务键</th><th>标题</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="b in current.items || []" :key="b.id">
            <td>{{ b.biz_key }}</td>
            <td>{{ b.title }}</td>
            <td>{{ b.status }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
