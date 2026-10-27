# Local Windows SAPI synthesis used ONLY when the user explicitly opts in to
# Microsoft-pretrained OS voices. Text is read from a file; no text is placed in
# the command line and no network access is used.
param(
    [Parameter(Mandatory=$true)][string]$TextPath,
    [Parameter(Mandatory=$true)][string]$WavPath
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($TextPath, [System.Text.Encoding]::UTF8)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SetOutputToWaveFile($WavPath)
    $synth.Speak($text)
} finally {
    $synth.Dispose()
}
