Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing

$projectRoot = Split-Path -Parent $PSScriptRoot
$outputDir = Join-Path $projectRoot "assets\personas"
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

$profiles = @(
    @{
        Key = "succubus"
        Name = "Nyx"
        Subtitle = "Succubus Servant"
        Top = "#1a0528"
        Bottom = "#8f1238"
        Accent = "#ff5d9e"
        Symbol = "horns"
    },
    @{
        Key = "milf_teacher"
        Name = "Ms. Vale"
        Subtitle = "Strict IELTS Mentor"
        Top = "#2a180f"
        Bottom = "#b56a2a"
        Accent = "#ffd28a"
        Symbol = "glasses"
    },
    @{
        Key = "soft_girlfriend"
        Name = "Mina"
        Subtitle = "Soft Companion"
        Top = "#1a2940"
        Bottom = "#e5a5b5"
        Accent = "#ffe7ef"
        Symbol = "moon"
    },
    @{
        Key = "rot_maiden"
        Name = "The Rot Maiden"
        Subtitle = "Path Companion"
        Top = "#30130f"
        Bottom = "#b63d25"
        Accent = "#f0c46a"
        Symbol = "blade"
    },
    @{
        Key = "final_boss_queen"
        Name = "Queen Obsidia"
        Subtitle = "Final Boss Queen"
        Top = "#090912"
        Bottom = "#49306b"
        Accent = "#d7b6ff"
        Symbol = "crown"
    }
)

function Convert-HexColor {
    param([string] $Hex)
    $clean = $Hex.TrimStart("#")
    return [System.Drawing.Color]::FromArgb(
        [Convert]::ToInt32($clean.Substring(0, 2), 16),
        [Convert]::ToInt32($clean.Substring(2, 2), 16),
        [Convert]::ToInt32($clean.Substring(4, 2), 16)
    )
}

function New-Pen {
    param([System.Drawing.Color] $Color, [float] $Width)
    $pen = New-Object System.Drawing.Pen($Color, $Width)
    $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    return $pen
}

foreach ($profile in $profiles) {
    foreach ($variant in 1..2) {
        $width = 1024
        $height = 1024
        $bitmap = New-Object System.Drawing.Bitmap($width, $height)
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit

        $rect = New-Object System.Drawing.Rectangle(0, 0, $width, $height)
        $top = Convert-HexColor $profile.Top
        $bottom = Convert-HexColor $profile.Bottom
        $accent = Convert-HexColor $profile.Accent
        $gradient = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
            $rect,
            $top,
            $bottom,
            (35 + ($variant * 18))
        )
        $graphics.FillRectangle($gradient, $rect)

        $rng = New-Object System.Random(($profile.Key.GetHashCode() + $variant * 1009))
        for ($i = 0; $i -lt 26; $i++) {
            $alpha = 18 + $rng.Next(34)
            $color = [System.Drawing.Color]::FromArgb($alpha, $accent)
            $brush = New-Object System.Drawing.SolidBrush($color)
            $size = 24 + $rng.Next(120)
            $x = $rng.Next(-60, $width)
            $y = $rng.Next(-60, $height)
            $graphics.FillEllipse($brush, $x, $y, $size, $size)
            $brush.Dispose()
        }

        $haloBrush = New-Object System.Drawing.SolidBrush(
            [System.Drawing.Color]::FromArgb(42, $accent)
        )
        $graphics.FillEllipse($haloBrush, 212, 96, 600, 600)
        $haloBrush.Dispose()

        $shadowBrush = New-Object System.Drawing.SolidBrush(
            [System.Drawing.Color]::FromArgb(185, 8, 8, 14)
        )
        $graphics.FillEllipse($shadowBrush, 292, 615, 440, 110)
        $graphics.FillEllipse($shadowBrush, 357, 270, 310, 310)
        $graphics.FillPie($shadowBrush, 262, 455, 500, 540, 200, 140)

        $accentPen = New-Pen $accent 18
        switch ($profile.Symbol) {
            "horns" {
                $graphics.DrawArc($accentPen, 285, 210, 170, 230, 185, 95)
                $graphics.DrawArc($accentPen, 570, 210, 170, 230, 260, 95)
            }
            "glasses" {
                $graphics.DrawEllipse($accentPen, 335, 390, 120, 80)
                $graphics.DrawEllipse($accentPen, 570, 390, 120, 80)
                $graphics.DrawLine($accentPen, 455, 430, 570, 430)
            }
            "moon" {
                $moonBrush = New-Object System.Drawing.SolidBrush($accent)
                $cutBrush = New-Object System.Drawing.SolidBrush($top)
                $graphics.FillEllipse($moonBrush, 625, 190, 115, 115)
                $graphics.FillEllipse($cutBrush, 660, 172, 115, 125)
                $moonBrush.Dispose()
                $cutBrush.Dispose()
            }
            "blade" {
                $graphics.DrawLine($accentPen, 690, 230, 420, 610)
                $graphics.DrawLine($accentPen, 460, 600, 350, 710)
            }
            "crown" {
                $points = @(
                    [System.Drawing.Point]::new(342, 310),
                    [System.Drawing.Point]::new(420, 205),
                    [System.Drawing.Point]::new(512, 315),
                    [System.Drawing.Point]::new(604, 205),
                    [System.Drawing.Point]::new(682, 310)
                )
                $graphics.DrawLines($accentPen, $points)
            }
        }

        $fontName = "Georgia"
        $titleFont = New-Object System.Drawing.Font($fontName, 68, [System.Drawing.FontStyle]::Bold)
        $subFont = New-Object System.Drawing.Font($fontName, 32, [System.Drawing.FontStyle]::Regular)
        $smallFont = New-Object System.Drawing.Font("Consolas", 22, [System.Drawing.FontStyle]::Regular)
        $textBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(245, 255, 250, 245))
        $accentBrush = New-Object System.Drawing.SolidBrush($accent)
        $center = New-Object System.Drawing.StringFormat
        $center.Alignment = [System.Drawing.StringAlignment]::Center

        $graphics.DrawString($profile.Name, $titleFont, $textBrush, [System.Drawing.RectangleF]::new(80, 742, 864, 92), $center)
        $graphics.DrawString($profile.Subtitle, $subFont, $accentBrush, [System.Drawing.RectangleF]::new(80, 835, 864, 55), $center)
        $graphics.DrawString("Daily Intel Persona", $smallFont, $textBrush, [System.Drawing.RectangleF]::new(80, 924, 864, 40), $center)

        $fileName = "{0}_{1:00}.png" -f $profile.Key, $variant
        $path = Join-Path $outputDir $fileName
        $bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)

        $center.Dispose()
        $textBrush.Dispose()
        $accentBrush.Dispose()
        $titleFont.Dispose()
        $subFont.Dispose()
        $smallFont.Dispose()
        $accentPen.Dispose()
        $shadowBrush.Dispose()
        $gradient.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

Write-Host "persona_cards_generated=$outputDir"
