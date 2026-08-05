# 오디오 평가셋 — 레포에 넣지 않는다

저작권·개인정보·용량 셋 다 걸린다. 매니페스트(`eval/asr-manifest.jsonl`)만
버전 관리하고, **오디오 파일은 여기에 각자 반입한다.**

## 형식

16 kHz · 모노 · 16-bit PCM WAV. 변환은 반입 전에 끝낸다 — 평가 도구가
변환까지 하면 그 변환이 결과에 섞인다.

```bash
ffmpeg -i 원본.flac -ar 16000 -ac 1 -c:a pcm_s16le eval/audio/zeroth-001.wav
```

## 무엇을 쓰나

| 데이터셋 | 성격 | 절차 |
|---|---|---|
| **Zeroth-Korean** | 낭독체. 무료, 바로 받을 수 있다 | 첫 기준선용 |
| KsponSpeech | 자유대화. 상담 음성에 가깝다 | 신청·승인 필요 |
| 고객사 녹취 | 가장 정확한 근거 | 동의·비식별화 선행 |

**Zeroth 는 낭독체다.** 상담·회의 음성과 발화 특성이 다르므로, 여기서 나온
CER 을 그대로 "상담 정확도"라고 부르면 안 된다. 첫 기준선이자 회귀 감지용이다.

## 돌리기

```bash
uv run evalctl run --suite stt --stt-model /path/to/faster-whisper-small
```

오디오가 없으면 **건너뛰지 않고 멈춘다.** 조용히 건너뛰면 "STT 품질 통과"로
읽히기 때문이다.
