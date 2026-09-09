<?php

declare(strict_types=1);

namespace Tests;

use App\Greeting;
use PHPUnit\Framework\TestCase;

final class GreetingTest extends TestCase
{
    public function testGreet(): void
    {
        $this->assertSame('Hello, World!', Greeting::greet('World'));
    }
}
