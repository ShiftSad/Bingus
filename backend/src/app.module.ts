import { Module } from '@nestjs/common';
import { AppController } from './app.controller';
import { AppService } from './app.service';
import { SearchModule } from './search/search.module';
import { ConfigModule } from '@nestjs/config';
import { ProxyModule } from './proxy/proxy.module';

@Module({
  imports: [
    ConfigModule.forRoot({
      isGlobal: true,
    }),
    SearchModule,
    ProxyModule
  ],
  controllers: [AppController],
  providers: [AppService],
})
export class AppModule {}
