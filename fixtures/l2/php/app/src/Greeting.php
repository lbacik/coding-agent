<?php

declare(strict_types=1);

namespace App;

final class Greeting
{
    public static function greet(string $name): string
    {
        return "Hello, {$name}!";
    }

    public static function shout(string $name): string
    {
        return strtoupper(self::greet($name));
    }
}
