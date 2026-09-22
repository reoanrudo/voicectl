# 動作確認用の日本語音声を Windows の音声合成で作る
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$ja = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ja-JP' } | Select-Object -First 1
$s.SelectVoice($ja.VoiceInfo.Name)
$out = Join-Path $PSScriptRoot 'audio'
New-Item -ItemType Directory -Force $out | Out-Null
$phrases = [ordered]@{
  '01_memo'   = 'メモ帳開いて'
  '02_right'  = 'もうちょい右'
  '03_click'  = 'そこクリック'
  '04_scroll' = '下にスクロール'
  '05_type'   = 'こんにちはと入力して'
  '06_save'   = '保存ボタンを押して'
  '07_number' = '5番'
  '08_stop'   = 'ストップ'
  '09_browser'= 'ブラウザ起動して'
  '10_copy'   = 'コピーして'
  '11_youtube'= 'ユーチューブと入力して'
  '12_photoshop' = 'フォトショップ開いて'
  '13_brave'  = 'ブレイブ起動して'
}
foreach ($k in $phrases.Keys) {
  $s.SetOutputToWaveFile((Join-Path $out "$k.wav"))
  $s.Speak($phrases[$k])
}
$s.SetOutputToNull()
