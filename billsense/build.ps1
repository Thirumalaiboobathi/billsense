# Packages the Lambda with Linux wheels from Windows. Do NOT use `sam build`.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$build = Join-Path $root ".build"
$app = Join-Path $root "app"

if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Path $build | Out-Null

Write-Host "Installing Linux wheels into .build ..."
uv pip install -r (Join-Path $app "requirements.txt") --target $build --python-platform x86_64-manylinux2014 --python-version 3.12 --only-binary :all:
if ($LASTEXITCODE -ne 0) {
    Write-Host "uv failed, falling back to pip ..."
    pip install -r (Join-Path $app "requirements.txt") -t $build --platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all: --implementation cp
    if ($LASTEXITCODE -ne 0) { throw "Dependency install failed" }
}

Copy-Item (Join-Path $app "*.py") $build
Copy-Item (Join-Path $app "index.html") $build

$mb = [math]::Round(((Get-ChildItem $build -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB, 1)
Write-Host "Build ready in .build ($mb MB unzipped; Lambda limit is 250 MB)"
