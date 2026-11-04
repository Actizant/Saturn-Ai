# Downloads the two OpenCV face models that the identity tier needs.
# Safe to run again: a file that is already big enough is left alone.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $root 'models\face'
New-Item -ItemType Directory -Force -Path $target | Out-Null

$base = 'https://github.com/opencv/opencv_zoo/raw/main/models'
$files = @(
    @{ Name = 'face_detection_yunet_2023mar.onnx'
       Url  = "$base/face_detection_yunet/face_detection_yunet_2023mar.onnx"
       Min  = 200000 },
    @{ Name = 'face_recognition_sface_2021dec.onnx'
       Url  = "$base/face_recognition_sface/face_recognition_sface_2021dec.onnx"
       Min  = 30000000 }
)

foreach ($file in $files) {
    $dest = Join-Path $target $file.Name
    if ((Test-Path $dest) -and ((Get-Item $dest).Length -ge $file.Min)) {
        Write-Host ("  ok       {0} already present" -f $file.Name) -ForegroundColor Green
        continue
    }
    Write-Host ("  fetching {0} ..." -f $file.Name) -ForegroundColor Cyan
    Invoke-WebRequest -Uri $file.Url -OutFile $dest -UseBasicParsing
    $size = (Get-Item $dest).Length
    if ($size -lt $file.Min) {
        $msg = "{0} came back at {1} bytes - the download was cut short. " -f $file.Name, $size
        throw ($msg + "Delete it and run this again.")
    }
    Write-Host ("  ok       {0} ({1:N0} bytes)" -f $file.Name, $size) -ForegroundColor Green
}

Write-Host ''
Write-Host 'Done. Files in models\face:' -ForegroundColor Green
Get-ChildItem $target -File | Select-Object Name, Length | Format-Table -AutoSize
